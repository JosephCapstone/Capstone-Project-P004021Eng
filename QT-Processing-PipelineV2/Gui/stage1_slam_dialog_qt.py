#!/usr/bin/env python3
"""
Stage 1: SLAM - fields only
=============================
Defines Stage 1's fields, and nothing else - the popup/panel container
classes, row builders, project-mode logic (pipeline label + the
"Choose from project..." picker button), and Run stubbing all live in
qt_stage_base.py now.

Stage1SlamFieldsMixin is used by two containers:
- Stage1SlamDialog: a standalone popup - run this file directly to
  test Stage 1's own layout in isolation. Pass pipeline=None (the
  default) for manual mode - there's no main window here to supply a
  real project_manager.PipelineHandle.
- Stage1SlamPanel: an embeddable panel - used by
  pipeline_applet_qt_template.py as a page of stageStack, passed the
  currently active Source pipeline (baseline or a scan), or None if no
  project is open yet - see add_pipeline_label()/
  add_project_picker_button() in qt_stage_base.py for how this panel
  still gains project-mode picking later if a project is created AFTER
  this panel was already built.

Run standalone:
    pip install PySide6
    python stage1_slam_dialog_qt.py
"""
import shutil
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QPushButton, QMessageBox, QLabel

from qt_stage_base import (QStageDialog, QStagePanel, SCRIPTS_DIR, CONFIGS_DIR, fmt_cm,
                           LineEditRef)

try:
    import project_manager as pm
except ImportError:
    pm = None

try:
    import pipeline_core as core
except ImportError:
    core = None


class BackendMirroredRef(LineEditRef):
    """One value, shown in two places: 'Map max range' has a text box in the
    KISS-ICP section (next to the min range, the config's own max range and
    the horizontal/vertical limits) and a text box in the Ouster CLI section
    (the only crop that backend has). The two boxes always show the same text,
    so the value stays when the Backend changes. get() reads the box of the
    selected Backend and set() writes both, so the rest of the code uses
    fields["map_max_range"] as before."""

    def __init__(self, kiss_edit, ouster_edit, backend_getter):
        self.kiss_edit = kiss_edit
        self.ouster_edit = ouster_edit
        self._backend_getter = backend_getter
        kiss_edit.textChanged.connect(lambda text: self._copy(ouster_edit, text))
        ouster_edit.textChanged.connect(lambda text: self._copy(kiss_edit, text))

    @staticmethod
    def _copy(target, text):
        if target.text() != text:
            target.setText(text)

    @property
    def widget(self):
        return self.kiss_edit if self._backend_getter() == "kiss_icp" else self.ouster_edit


class Stage1SlamFieldsMixin:

    def _build_slam_fields(self, pipeline=None):
        # Always called, even with pipeline=None - see qt_stage_base.py's
        # module docstring for why (activation later, without a rebuild).
        self.add_pipeline_label(pipeline)

        self.add_radio_choice("backend", "Backend:", [
            ("KISS-ICP - reads PointCloud2 rosbag topics (Ouster or Hesai driver). "
             "Raw Ouster packets must be decoded first (see 'Check Source for Raw "
             "Packets...' below).", "kiss_icp"),
            ("Ouster CLI - Ouster sensors only. Reads raw Ouster packets "
             "(pcap/OSF/rosbag) directly. Cannot read a Hesai bag.", "ouster"),
        ], default="kiss_icp")
        self.add_help(
            "backend",
            "The program that builds the map from the recording.",
            effects=[
                ("KISS-ICP", "reads PointCloud2 topics from a rosbag. It works with an "
                             "Ouster or a Hesai driver. Raw Ouster packets must be decoded "
                             "first."),
                ("Ouster CLI", "reads raw Ouster packets (pcap, OSF, rosbag) directly. No "
                               "decoding is necessary. It works with Ouster sensors only."),
            ],
            when="Use KISS-ICP for a Hesai sensor or for a bag with decoded points. Use "
                 "Ouster CLI for a .pcap or .osf file. To compare the two backends, give "
                 "both the same source and a different output name.")

        self.add_file_or_folder_field(
            "source", "Source (pcap / OSF / .bag / ROS2 bag folder):",
            [("PCAP files", "*.pcap"), ("OSF files", "*.osf"), ("ROS bag files", "*.bag"),
             ("All files", "*.*")],
            on_picked=self._check_raw)
        self.add_help(
            "source",
            "The recording that Stage 1 turns into a map. Both backends detect the type "
            "from the file or folder.",
            effects=[
                ("File...", "select a .pcap, .osf or ROS1 .bag file."),
                ("Folder...", "select a ROS2 bag folder (it holds a .db3 file and "
                              "metadata.yaml). Stage 1 checks the folder for raw Ouster "
                              "packets and offers to convert it."),
            ],
            when="Change it to map another recording. A ROS bag needs the 'rosbags' "
                 "Python package with both backends. To repeat the raw-packet check, "
                 "use 'Check Source for Raw Packets...'.")
        self.add_project_picker_button(
            "source", lambda: pm.list_eligible_inputs(self.pipeline, "slam"),
            extra_on_pick=self._check_raw)

        check_btn = QPushButton("Check Source for Raw Packets...")
        check_btn.clicked.connect(
            lambda: self._check_raw(self.fields["source"].get(), interactive=True))
        self.form.addWidget(check_btn)

        output_default = self.resolve_project_output_default(pipeline, "slam", ".ply")
        self.add_save_field("output", "Output .ply:", default_ext=".ply", default=output_default)
        self.register_auto_default(
            "output", lambda p: self.resolve_project_output_default(p, "slam", ".ply"))
        self.add_help(
            "output",
            "The file that Stage 1 writes. It holds the map.",
            when="Change it to save under another name. In project mode the name comes "
                 "from the pipeline.")

        # 'Map max range' is not here any more: it sits with the other range
        # limits inside each Backend's own section (see BackendMirroredRef).

        # Only the section for the selected Backend is visible - mirrors
        # begin_section()/end_section() + grid_remove()/grid() in the
        # Tkinter version, using show()/hide() instead.
        self.ouster_section = self.begin_section()
        # Update 28: the Meta JSON file is a Basic field for Ouster CLI (a .pcap
        # needs it) and an Advanced field for KISS-ICP (see below). One value,
        # two boxes: fields["meta"] reads the box of the selected Backend.
        ouster_meta_edit = self.add_file_field(
            "meta_ouster", "Meta JSON file (optional for rosbag sources):",
            [("JSON files", "*.json"), ("All files", "*.*")])
        self.add_help(
            ouster_meta_edit,
            "The sensor metadata file (.json) of the recording.",
            when="The source is a .pcap file: it needs this file. A rosbag source does "
                 "not need it. Stage 1 reads the metadata from the bag when the box is "
                 "empty.")
        self.add_preset_selector("Voxel size preset:", [
            ("Fine (15 cm) - slower, most detail", {"voxel_size": "0.15"}),
            ("Medium (25 cm) - balanced, good default", {"voxel_size": "0.25"}),
            ("Coarse (50 cm) - fastest, least detail", {"voxel_size": "0.5"}),
        ])
        # Preset values stay in METRES - LengthFieldRef.set() shows them in cm.
        self.add_length_field("voxel_size", "Voxel size (cm):", default_m="0.25",
                              min_cm=1, max_cm=300)
        self.add_help(
            "voxel_size",
            "The size of the grid cell that SLAM uses to thin the points. One point stays "
            "in each cell.",
            effects=[
                ("Smaller (15 cm)", "more detail. The run is slower and the output file "
                                    "is larger."),
                ("Larger (50 cm)", "the run is faster. The map has less detail."),
            ],
            when="The map is too coarse, or the run is too slow. Medium (25 cm) is the "
                 "normal value.")
        # Settings that normally stay at their default values (closed block).
        self.ouster_advanced = self.begin_advanced("Advanced")
        ouster_range_edit = self.add_text_field("_ouster_map_max_range",
                                                "Map max range (m, optional):")
        del self.fields["_ouster_map_max_range"]   # reached through fields["map_max_range"]
        self.add_help(
            ouster_range_edit,
            "Removes far points from the saved map. The map does not include points that "
            "are farther than this distance from the sensor (the ouster-cli 'clip' step "
            "after 'slam'). SLAM registration still uses the full sensor range.",
            effects=[
                ("Smaller", "more far noise is removed. Surfaces behind the limit are "
                            "also removed."),
                ("Larger or empty", "more points stay in the map. Empty keeps all points."),
            ],
            when="Far noise shows in the map, for example in an open area. Use a value a "
                 "little larger than the distance from the robot path to the farthest "
                 "surface that you must keep.",
            note="This backend has no separate horizontal and vertical limits.")
        self.add_checkbox("visualize", "Open visualizer after processing")
        self.add_help(
            "visualize",
            "Opens the Ouster visualizer when the processing ends.",
            effects=[
                ("On", "a viewer window shows the result."),
                ("Off", "no window opens."),
            ],
            when="You want to look at the map directly after the run.")
        self.end_advanced()
        self.end_section()

        self.kiss_section = self.begin_section()
        bundled_config = CONFIGS_DIR / "kiss_icp_config_indoor.yaml"
        hesai_config = CONFIGS_DIR / "kiss_icp_config_hesai.yaml"
        sensor_presets = [("Keep the Config field as it is", {})]
        if bundled_config.exists():
            sensor_presets.append(("Ouster OS0-128 - kiss_icp_config_indoor.yaml",
                                   {"kiss_icp_config": str(bundled_config)}))
        if hesai_config.exists():
            sensor_presets.append(("Hesai XT32 - kiss_icp_config_hesai.yaml (not tuned)",
                                   {"kiss_icp_config": str(hesai_config)}))
        if len(sensor_presets) > 1:
            self.sensor_preset_combo = self.add_preset_selector(
                "Sensor preset:", sensor_presets)
        # Settings that normally stay at their default values (closed block).
        # Update 28: the Config .yaml path and the Meta JSON file are inside.
        self.kiss_advanced = self.begin_advanced("Advanced")
        kiss_meta_edit = self.add_file_field(
            "meta_kiss", "Meta JSON file (optional):",
            [("JSON files", "*.json"), ("All files", "*.*")])
        self.add_help(
            kiss_meta_edit,
            "The sensor metadata file (.json) of the recording.",
            when="The source is a .pcap file (the 'ouster' dataloader). A rosbag source "
                 "does not need it.")
        config_edit = self.add_file_field(
            "kiss_icp_config", "Config .yaml (recommended):",
            [("YAML files", "*.yaml *.yml"), ("All files", "*.*")],
            default=str(bundled_config) if bundled_config.exists() else "")
        self.add_help(
            config_edit,
            "The KISS-ICP settings file (.yaml). It sets the voxel size, the ranges and "
            "other SLAM values. The Sensor preset puts the file for the sensor in this "
            "box.",
            effects=[
                ("With a config", "KISS-ICP uses the values for an indoor map. The "
                                  "bundled kiss_icp_config_indoor.yaml is the default."),
                ("Without a config", "KISS-ICP uses defaults for an outdoor vehicle. In a "
                                     "compartment the map then has far fewer points."),
            ],
            when="Use another file for another sensor, or for your own tuned values. The "
                 "Hesai config changes one value of the Ouster config (min_range 0.1). Its "
                 "other values are not tuned for Hesai.",
            note="Keep this box filled for a map of a compartment.")

        self.add_file_field("kiss_icp_script", "Script (.py):", [("Python files", "*.py")],
                             default=str(SCRIPTS_DIR / "slam_kiss_icp.py"))
        self.add_help(
            "kiss_icp_script",
            "The Python script that runs KISS-ICP.",
            when="Change it only to use a changed copy of the script.")
        self.kiss_voxel_size_label = QLabel()
        self.kiss_voxel_size_label.setWordWrap(True)
        self.form.addWidget(self.kiss_voxel_size_label)
        self.add_length_field("kiss_icp_voxel_size", "Voxel size override (cm, optional):",
                              min_cm=0.5, max_cm=100)
        self.add_help(
            "kiss_icp_voxel_size",
            "Replaces the voxel size of the config for this run. The voxel is the grid "
            "cell that SLAM uses to thin the points.",
            effects=[
                ("Smaller", "more detail. The run is slower and the output file is "
                            "larger."),
                ("Larger", "the run is faster. The map has less detail."),
            ],
            when="The map is too coarse, or the run is too slow. Leave the box empty to "
                 "use the voxel size of the config (shown above the box), or the KISS-ICP "
                 "default if there is no config.")

        range_heading = QLabel("Range limits")
        range_heading.setStyleSheet("font-weight: bold; margin-top: 6px;")
        self.form.addWidget(range_heading)

        self.kiss_min_range_label = QLabel()
        self.kiss_min_range_label.setWordWrap(True)
        self.form.addWidget(self.kiss_min_range_label)
        self.add_text_field("kiss_icp_min_range", "Min range override (m, optional):")
        self.add_help(
            "kiss_icp_min_range",
            "Removes points that are closer to the sensor than this distance, before SLAM "
            "runs.",
            effects=[
                ("Smaller", "more near points stay. Parts of the sensor mount can stay in "
                            "the data."),
                ("Larger", "more near points are removed. Real surfaces near the sensor "
                           "are also removed."),
            ],
            when="The sensor sees part of its own mount. It shows as a dense blob that "
                 "does not move, and it can confuse SLAM. Start with 0.2-0.3 m and raise "
                 "the value only as needed. Leave the box empty to use the value of the "
                 "config, or no crop if the config has none.")

        self.kiss_max_range_label = QLabel()
        self.kiss_max_range_label.setWordWrap(True)
        self.form.addWidget(self.kiss_max_range_label)
        kiss_range_edit = self.add_text_field("map_max_range", "Map max range (m, optional):")
        self.add_help(
            kiss_range_edit,
            "Replaces the max_range of the config (shown above the box) for this run. SLAM "
            "registration uses this range. The saved map does not include points that are "
            "farther than this distance from the sensor. The distance is measured from "
            "the sensor position of each scan along the whole path, not from the start.",
            effects=[
                ("Smaller", "far noise is removed. SLAM has less of the room to match."),
                ("Larger", "SLAM matches more of the room. Far noise stays."),
            ],
            when="Far noise shows in the map. Leave the box empty to use the max_range of "
                 "the config. For more control of the saved map only, use the horizontal "
                 "and vertical limits below.")

        self.add_text_field("kiss_icp_map_max_horizontal",
                            "Map max horizontal distance (m, optional):")
        self.add_help(
            "kiss_icp_map_max_horizontal",
            "The saved map does not include points that are farther than this distance "
            "from the sensor along the floor. KISS-ICP only.",
            effects=[
                ("Smaller", "more far points are removed."),
                ("Larger or empty", "more far points stay."),
            ],
            when="Far noise shows in an open area, and you must keep a high ceiling.",
            note="A point must be inside all the limits that you set, also 'Map max range'.")
        self.add_text_field("kiss_icp_map_max_vertical",
                            "Map max vertical distance (m, optional):")
        self.add_help(
            "kiss_icp_map_max_vertical",
            "The saved map does not include points that are farther than this distance "
            "from the sensor up or down. 'Vertical' is the up direction of the sensor at "
            "the start of the scan. KISS-ICP only.",
            effects=[
                ("Smaller", "more points far above or below the sensor are removed."),
                ("Larger or empty", "more points stay."),
            ],
            when="Points far above or below the sensor must go from the map.",
            note="A point must be inside all the limits that you set, also 'Map max range'.")

        self.add_text_field("kiss_icp_dataloader", "Force dataloader (optional):")
        self.add_help(
            "kiss_icp_dataloader",
            "The KISS-ICP reader for the type of the source.",
            when="Stage 1 reads the wrong type. Leave the box empty to detect the type "
                 "from the source.")
        self.add_text_field("kiss_icp_topic", "Rosbag topic (optional):")
        self.add_help(
            "kiss_icp_topic",
            "The name of the PointCloud2 topic to read from the rosbag.",
            when="The bag has more than one PointCloud2 topic. Leave the box empty when "
                 "the bag has one.")

        self.add_checkbox("kiss_icp_flying_pixel_filter",
                          "Remove flying pixels before SLAM", default=False)
        self.add_help(
            "kiss_icp_flying_pixel_filter",
            "A flying pixel is a false point in the air between a near surface and a far "
            "surface. The filter removes a point when its range is very different from "
            "the range of both of its neighbours in the scan.",
            effects=[
                ("On", "the filter removes flying pixels before SLAM."),
                ("Off", "all points go to SLAM."),
            ],
            when="The map has false points at the edges of near objects.",
            note="The filter needs the scan in row order with a time value for each point, "
                 "as the Ouster sensor gives it. A scan from a Hesai sensor, from the "
                 "simulator, or with no time value is not filtered, and the run output "
                 "says so. To check the effect, make one map with the option and one "
                 "without it, and compare them in CloudCompare.")
        self.end_advanced()
        self.end_section()

        self.fields["meta"] = BackendMirroredRef(
            kiss_meta_edit, ouster_meta_edit, lambda: self.fields["backend"].get())
        self.fields["map_max_range"] = BackendMirroredRef(
            kiss_range_edit, ouster_range_edit, lambda: self.fields["backend"].get())
        # The mirrored field belongs to both Advanced blocks (counted for the
        # block of the selected Backend).
        for advanced_info in (self.ouster_advanced, self.kiss_advanced):
            advanced_info["keys"].append("map_max_range")
            self.reset_advanced_baseline(advanced_info)

        config_edit.textChanged.connect(self._update_kiss_icp_config_labels)
        self._update_kiss_icp_config_labels()  # populate for the bundled-config default

        self.fields["backend"].trace_add("write", self._update_backend_sections)
        self._update_backend_sections()

        self.form.addStretch(1)

    def _update_kiss_icp_config_labels(self):
        """Refreshes both live "this config's own <field>" labels
        whenever the Config .yaml field changes - reads the file fresh
        each time via pipeline_core.py's read_kiss_icp_voxel_size()/
        read_kiss_icp_min_range(), same functions _build_run() already
        uses to compute the run report's own voxel_size/min_range
        lines, so the live label and the eventual report never
        disagree."""
        self._set_kiss_icp_config_label(
            self.kiss_voxel_size_label, "voxel_size",
            core.read_kiss_icp_voxel_size if core else None, as_cm=True)
        self._set_kiss_icp_config_label(
            self.kiss_min_range_label, "min_range",
            core.read_kiss_icp_min_range if core else None)
        self._set_kiss_icp_config_label(
            self.kiss_max_range_label, "max_range",
            core.read_kiss_icp_max_range if core else None)

    def _set_kiss_icp_config_label(self, label, field_name, reader, as_cm=False):
        config = self.fields["kiss_icp_config"].get().strip()
        if reader is None:
            label.setText(f"This config's own {field_name}: pipeline_core.py not available")
            label.setStyleSheet("color: #777777; font-size: 8pt;")
            return
        if not config:
            label.setText(f"This config's own {field_name}: (no config file given)")
            label.setStyleSheet("color: #777777; font-size: 8pt;")
            return
        try:
            value = reader(config)
        except Exception as e:
            label.setText(f"This config's own {field_name}: could not read config ({e})")
            label.setStyleSheet("color: #bb0000; font-size: 8pt;")
            return
        if value is None:
            label.setText(f"This config's own {field_name}: not set in this config")
            label.setStyleSheet("color: #777777; font-size: 8pt;")
            return
        shown = (f"{fmt_cm(value)} (the file stores {value} m)" if as_cm else f"{value} m")
        label.setText(f"This config's own {field_name}: {shown}")
        label.setStyleSheet("color: #009955; font-size: 8pt;")

    def _update_backend_sections(self):
        if self.fields["backend"].get() == "kiss_icp":
            self.ouster_section.hide()
            self.kiss_section.show()
        else:
            self.kiss_section.hide()
            self.ouster_section.show()

    def _check_raw(self, path, interactive=False):
        """Real "any raw Ouster packets in here?" check - calls
        pipeline_core.inspect_rosbag_topics(path) directly (a plain
        Python read of the bag's topic list, not a subprocess). Actually
        converting a bag that has raw packets (decode_raw_packets.py,
        run as a subprocess) stays stubbed, same precedent as Run
        itself and Stage 5's Check Point Spacing.

        Confirmed safe to wire this far by PROJECT_INPUT_PICKER_PLAN.md
        Section 6.2: this check's own triggering conditions and
        internal logic are UNCHANGED by the ProjectFilePicker redesign
        - only which mechanism hands it a path changed (Browse/File/
        Folder, or now also "Choose from project...").

        interactive=False (the default, used right after Source is
        picked via Browse/File/Folder/"Choose from project...") stays
        quiet when there's nothing to report, so browsing isn't noisy.
        interactive=True (the explicit "Check Source for Raw
        Packets..." button) always shows something, even "nothing to
        do", so the button never looks like it did nothing."""
        if core is None or not path:
            return
        try:
            info = core.inspect_rosbag_topics(path)
        except RuntimeError as e:
            QMessageBox.warning(self, "Could not check for raw packets", str(e))
            return
        if info is None:
            if interactive:
                QMessageBox.information(
                    self, "Not a ROS2 bag folder",
                    f"'{Path(path).name}' doesn't look like a ROS2 bag folder (no "
                    f"metadata.yaml found) - nothing to check.")
            return
        if info["has_pointcloud2"] or not info["raw_lidar_topic"]:
            if interactive:
                QMessageBox.information(
                    self, "No raw packets found",
                    f"'{Path(path).name}' already has a decoded points topic, or no raw "
                    f"lidar packet topic was found - nothing to convert.")
            return
        if not info["metadata_topic"]:
            QMessageBox.warning(
                self, "Raw packets found, but no metadata topic",
                f"This bag has raw packets on '{info['raw_lidar_topic']}' "
                f"({info['raw_lidar_count']} packets), but no metadata topic (a "
                f"std_msgs/String topic with 'metadata' in its name) to decode them "
                f"with. Convert by hand with decode_raw_packets.py, passing "
                f"--metadata-topic explicitly, or point KISS-ICP at a bag that already "
                f"has decoded points.")
            return

        proceed = QMessageBox.question(
            self, "Raw packets detected",
            f"'{Path(path).name}' has raw Ouster packets on '{info['raw_lidar_topic']}' "
            f"({info['raw_lidar_count']} packets), not decoded points. If Backend is "
            f"KISS-ICP, its rosbag dataloader needs decoded points and will fail on this "
            f"bag as-is. If Backend is Ouster CLI, it can already read these raw packets "
            f"directly - converting is optional there, but still useful to have a "
            f"decoded copy on hand.\n\n"
            f"Convert to a new decoded bag now, using decode_raw_packets.py?",
            QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes
        if not proceed:
            return

        self._decode_raw_bag(path, info)

    def _decode_raw_bag(self, path, info):
        """Real conversion - matches _check_and_convert_raw_bag()'s own
        conversion half in pipeline_applet.py, simplified for the
        current (post-ProjectFilePicker-redesign) rules: the dialog
        field is always authoritative now, in project mode and manual
        mode alike, so no more auto-resolve-vs-manual branching
        (force_manual_input_mode(), the project_manual_override check)
        is needed. project_manager.set_decoded_raw_path() IS still
        called though, via _apply_decoded_source() below - that call
        was never about auto-resolve at all, it is what makes a
        decoded bag show up as a choice in "Choose from project..."
        afterward (see that method's own docstring). Also fixed here:
        the script's location resolves via SCRIPTS_DIR, not relative to
        this file's own folder - see the "structural note" from chat
        about that exact regression class in the real Tkinter app."""
        output_bag = Path(path).parent / f"{Path(path).name}_decoded"
        if output_bag.exists():
            # Update 26: a decoded bag from before update 26 has no point time,
            # so KISS-ICP would run without deskew. Offer to convert again.
            # The old folder is renamed, never deleted.
            if core.decoded_bag_has_point_time(output_bag) is False:
                old_name = self._unused_sibling_name(output_bag, "_no_time")
                answer = QMessageBox.question(
                    self, "Decoded bag has no point time",
                    f"A decoded bag already exists at:\n{output_bag}\n\n"
                    "This bag has no time for each point. It was made with an older version of the converter. "
                    "With this bag, KISS-ICP runs without deskew. When the robot moves or "
                    "turns, the walls in the map can be skewed.\n\n"
                    "Convert the raw bag again?\n"
                    f"Yes: the old decoded bag is renamed to:\n{old_name}\n"
                    "(it is not deleted), and the conversion starts.\n"
                    "No: use the existing decoded bag.",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
                if answer == QMessageBox.Yes:
                    try:
                        output_bag.rename(old_name)
                    except OSError as e:
                        QMessageBox.critical(
                            self, "Cannot rename the old decoded bag",
                            f"The old decoded bag was not changed.\n\n{e}")
                        return
                    self._report_output(f"Renamed the old decoded bag to: {old_name}")
            if output_bag.exists():
                self._apply_decoded_source(output_bag)
                QMessageBox.information(
                    self, "Already converted",
                    f"A decoded bag already exists at:\n{output_bag}\n\n"
                    "Source has been updated to use it.")
                return

        script = SCRIPTS_DIR / "decode_raw_packets.py"
        if not script.exists():
            QMessageBox.critical(
                self, "Missing script",
                f"decode_raw_packets.py not found in:\n{SCRIPTS_DIR}")
            return

        cmd = core.build_decode_command(
            script, path, output_bag, lidar_topic=info.get("raw_lidar_topic"),
            imu_topic=info.get("raw_imu_topic"), metadata_topic=info.get("metadata_topic"))

        def on_decode_complete(returncode, cancelled):
            if returncode == 0 and not cancelled:
                self._apply_decoded_source(output_bag)
                self._show_success(
                    "Converted",
                    f"Decoded bag saved to:\n{output_bag}\n\n"
                    "Source has been updated to use it.")
                return
            # output_bag did not exist before this run (checked above), so
            # anything there now is this run's incomplete output. Remove
            # it: otherwise the "Already converted" check above would
            # accept the incomplete bag as a finished conversion next time.
            removed = self._remove_partial_output(output_bag)
            reason = ("You stopped the conversion." if cancelled else
                      f"decode_raw_packets.py exited with code {returncode}. See the "
                      f"Terminal tab for details.")
            QMessageBox.critical(
                self, "Conversion not complete",
                reason + ("\n\nThe incomplete decoded bag was removed." if removed else ""))

        # Runs with the same lock and Stop support as a stage run - before
        # this, the decode ran with no "running" status, so another Run
        # could start on the same bag while it was still being written.
        self._run_utility_command(cmd, on_decode_complete)

    @staticmethod
    def _unused_sibling_name(path, suffix):
        """<path><suffix>, or <path><suffix>_2, _3, ... - the first name that is free."""
        path = Path(path)
        candidate = path.with_name(path.name + suffix)
        number = 2
        while candidate.exists():
            candidate = path.with_name(f"{path.name}{suffix}_{number}")
            number += 1
        return candidate

    def _optional_positive_meters(self, key, name):
        """Blank -> None. Otherwise a number > 0 in metres, or ValueError."""
        text = self.fields[key].get().strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            raise ValueError(f"{name} must be a number in metres, for example 8, or blank.")
        if value <= 0:
            raise ValueError(f"{name} must be greater than 0, or blank.")
        return value

    def _remove_partial_output(self, path):
        path = Path(path)
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
            else:
                return False
        except OSError as e:
            self._report_output(f"WARNING: could not remove incomplete output {path}: {e}")
            return False
        self._report_output(f"Removed incomplete output: {path}")
        return True

    def _apply_decoded_source(self, output_bag):
        """Fills the Source field with the decoded bag and, if a
        project pipeline is active, ALSO records it on the pipeline
        itself via project_manager.set_decoded_raw_path() - not for
        auto-resolve (that mechanism is gone), but so this decoded copy
        actually shows up as a pickable option in "Choose from
        project..." afterward. list_eligible_inputs()'s "raw" group
        reads raw["decoded_path"] specifically to decide whether a
        decoded entry exists to list at all - without this call, a
        manually decoded bag would update the field for THIS run but
        never become choosable from the project picker on a later
        visit to this stage."""
        self.fields["source"].set(str(output_bag))
        if self.pipeline is not None and pm is not None:
            try:
                pm.set_decoded_raw_path(self.pipeline, output_bag)
            except Exception as e:
                # Deliberately broad, not just pm.ProjectError - this
                # call's most likely real-world failure is
                # set_decoded_raw_path()'s internal _to_relative()
                # raising because output_bag isn't actually under this
                # project's root (e.g. the raw import used a "reference"
                # link_raw mode rather than "copy", so the decoded copy
                # written next to it also falls outside project.root).
                # A narrower except here would let that propagate
                # uncaught out of a Qt slot - PySide6 tends to print it
                # to stderr and otherwise swallow it silently, which is
                # a strictly worse outcome than a visible warning
                # dialog naming the real exception.
                QMessageBox.warning(
                    self, "Could not record decoded path",
                    f"The Source field was updated, but recording this on the "
                    f"project failed, so it won't show up in 'Choose from "
                    f"project...' later:\n{e}")

    def _build_run(self):
        """Real Run - matches open_slam_dialog()'s build() in
        pipeline_applet.py exactly: same validation, same backend
        branching (Ouster CLI / KISS-ICP), same report text, same
        finish_info shape. Returning (cmd, report, finish_info) here
        (instead of returning nothing, like _stub_run's silent stub
        path) is what makes qt_stage_base.py's _on_run_clicked() take
        the real-execution path instead of falling back to the stub."""
        backend = self.fields["backend"].get()
        source = self.require_existing_file("source", "Source")
        if backend == "ouster" and core is not None:
            can_read, reason = core.ouster_cli_can_read(source)
            if not can_read:
                raise ValueError(reason)
        meta = self.fields["meta"].get().strip() or None
        output = self.require("output", "Output .ply")
        active_pipeline = self.get_active_pipeline_for_run()
        finish_info = {"pipeline": active_pipeline, "stage_name": "slam", "output": output}
        map_max_range_text = self.fields["map_max_range"].get().strip()
        if map_max_range_text:
            try:
                map_max_range = float(map_max_range_text)
            except ValueError:
                raise ValueError("Map max range must be a number, for example 8, or blank.")
            if map_max_range <= 0:
                raise ValueError("Map max range must be greater than 0, or blank.")
        else:
            map_max_range = None
        if backend == "kiss_icp":
            map_max_range_line = (
                f"Map max range: {map_max_range} m (replaces the config's max_range: "
                f"used for SLAM registration and for the saved map)\n"
                if map_max_range is not None else "Map max range: not used\n")
        else:
            map_max_range_line = (f"Map max range: {map_max_range} m (saved map only)\n"
                                  if map_max_range is not None else "Map max range: not used\n")

        if backend == "kiss_icp":
            script = self.require("kiss_icp_script", "KISS-ICP script")
            config = self.fields["kiss_icp_config"].get().strip() or None
            dataloader = self.fields["kiss_icp_dataloader"].get().strip() or None
            topic = self.fields["kiss_icp_topic"].get().strip() or None
            voxel_size_text = self.fields["kiss_icp_voxel_size"].get().strip()
            if voxel_size_text:
                try:
                    kiss_voxel_size = float(voxel_size_text)
                except ValueError:
                    raise ValueError("Voxel size override must be a number in cm, for "
                                      "example 8, or blank to use the config's own value.")
            else:
                kiss_voxel_size = None
            min_range_text = self.fields["kiss_icp_min_range"].get().strip()
            if min_range_text:
                try:
                    kiss_min_range = float(min_range_text)
                except ValueError:
                    raise ValueError("Min range override must be a number, e.g. 0.3, or "
                                      "left blank to use the config's own value.")
            else:
                kiss_min_range = None
            map_max_horizontal = self._optional_positive_meters(
                "kiss_icp_map_max_horizontal", "Map max horizontal distance")
            map_max_vertical = self._optional_positive_meters(
                "kiss_icp_map_max_vertical", "Map max vertical distance")
            flying_pixel_filter = bool(self.fields["kiss_icp_flying_pixel_filter"].get())
            cmd = core.build_kiss_icp_slam_command(
                script, source, output, config=config, dataloader=dataloader,
                topic=topic, meta=meta, voxel_size=kiss_voxel_size, min_range=kiss_min_range,
                map_max_range=map_max_range, map_max_horizontal=map_max_horizontal,
                map_max_vertical=map_max_vertical, pipeline=active_pipeline,
                flying_pixel_filter=flying_pixel_filter)

            effective_voxel_size = (kiss_voxel_size if kiss_voxel_size is not None
                                     else core.read_kiss_icp_voxel_size(config))
            effective_min_range = (kiss_min_range if kiss_min_range is not None
                                    else core.read_kiss_icp_min_range(config))
            report = (
                "=== SUMMARY ===\n"
                f"Backend: KISS-ICP\n"
                f"Source: {source}\n"
                + (f"Config: {config}\n" if config else
                   "No config given - using kiss-icp's vehicle-scale defaults, which will "
                   "likely under-populate an indoor map. Strongly consider adding one.\n")
                + (f"Voxel size override: {fmt_cm(kiss_voxel_size)}\n"
                   if kiss_voxel_size is not None
                   else f"Voxel size (from config): {fmt_cm(effective_voxel_size)}\n"
                   if effective_voxel_size is not None
                   else "Voxel size: unknown (no override, and none read from the config - "
                        "kiss-icp's own auto-derived default applies)\n")
                + (f"Min range override: {kiss_min_range} m\n" if kiss_min_range is not None
                   else f"Min range (from config): {effective_min_range} m\n"
                   if effective_min_range is not None
                   else "Min range: none set (no override, and none read from the config - "
                        "no near-sensor cropping will be applied)\n")
                + map_max_range_line
                + (f"Map max horizontal distance: {map_max_horizontal} m (saved map only)\n"
                   if map_max_horizontal is not None else "")
                + (f"Map max vertical distance: {map_max_vertical} m (saved map only)\n"
                   if map_max_vertical is not None else "")
                + ("Flying-pixel filter: on (see the run output for the number of points "
                   "removed, and for the frames that could not be filtered)\n"
                   if flying_pixel_filter else "")
                + f"Saved to: {output}\n\n"
                "=== NOTE ===\n"
                "Check the tool output above for the actual point count - if it's in the "
                "low thousands for what should be a substantial capture, that's the "
                "no-config under-population issue, not a bug in this run.\n\n"
                "=== NEXT STEPS ===\n"
                "Open it in CloudCompare to check map quality, or take it into Stage 2 "
                "(Level) to correct any tilt before cleanup."
            )
            return cmd, report, finish_info

        try:
            voxel_size = float(self.fields["voxel_size"].get())
        except ValueError:
            raise ValueError("Voxel size must be a number in cm, for example 25.")
        visualize = self.fields["visualize"].get()
        cmd = core.build_slam_command(source, voxel_size, output, meta=meta,
                                       visualize=visualize, map_max_range=map_max_range,
                                       pipeline=active_pipeline)

        report = (
            "=== SUMMARY ===\n"
            f"Backend: Ouster CLI\n"
            f"Source: {source}\n"
            + (f"Meta: {meta}\n" if meta else
               "No meta file given - resolved from the source itself.\n")
            + f"Saved to: {output}\n"
            f"Voxel size used: {fmt_cm(voxel_size)}\n"
            + map_max_range_line
            + f"Visualizer: {'opened' if visualize else 'not opened'}\n\n"
            + ("=== NOTE ===\n"
               "Map max range with the Ouster CLI backend uses ouster-cli 'clip' after "
               "'slam'. Look at the map in CloudCompare and make sure that the far "
               "points are gone.\n\n"
               if map_max_range is not None else "")
            + "=== NEXT STEPS ===\n"
            "Open it in CloudCompare to check map quality, "
            "or take it into Stage 2 (Level) to correct any tilt before cleanup."
        )
        return cmd, report, finish_info


class Stage1SlamDialog(QStageDialog, Stage1SlamFieldsMixin):
    """Standalone popup - see the module docstring. Run this file
    directly to open just this dialog."""

    def __init__(self, parent=None, pipeline=None, on_output=None, on_status=None):
        super().__init__("Stage 1: SLAM", parent, on_output=on_output, on_status=on_status)
        self._build_slam_fields(pipeline=pipeline)


class Stage1SlamPanel(QStagePanel, Stage1SlamFieldsMixin):
    """Embeddable panel - see the module docstring. Used by
    pipeline_applet_qt_template.py as a page of stageStack."""

    def __init__(self, parent=None, pipeline=None, on_output=None, on_status=None):
        super().__init__(parent, on_output=on_output, on_status=on_status)
        self._build_slam_fields(pipeline=pipeline)


def main():
    app = QApplication(sys.argv)
    dlg = Stage1SlamDialog(pipeline=None)
    dlg.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
