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

from qt_stage_base import QStageDialog, QStagePanel, SCRIPTS_DIR, CONFIGS_DIR, fmt_cm

try:
    import project_manager as pm
except ImportError:
    pm = None

try:
    import pipeline_core as core
except ImportError:
    core = None


class Stage1SlamFieldsMixin:

    def _build_slam_fields(self, pipeline=None):
        # Always called, even with pipeline=None - see qt_stage_base.py's
        # module docstring for why (activation later, without a rebuild).
        self.add_pipeline_label(pipeline)

        self.add_radio_choice("backend", "Backend:", [
            ("Ouster CLI - reads raw sensor packets directly (pcap/OSF/rosbag) - "
             "no decoding needed", "ouster"),
            ("KISS-ICP - different algorithm entry point, reads already-decoded "
             "PointCloud2 rosbag topics only - a source with only raw packets must "
             "be decoded first (see 'Check Source for Raw Packets...' below)", "kiss_icp"),
        ], default="kiss_icp", hint=("Both backends can point at the same source file for "
                 "side-by-side comparison - just switch Backend and the output filename."))

        self.add_file_or_folder_field(
            "source", "Source (pcap / OSF / .bag / ROS2 bag folder):",
            [("PCAP files", "*.pcap"), ("OSF files", "*.osf"), ("ROS bag files", "*.bag"),
             ("All files", "*.*")],
            on_picked=self._check_raw)
        self.add_project_picker_button(
            "source", lambda: pm.list_eligible_inputs(self.pipeline, "slam"),
            extra_on_pick=self._check_raw)

        check_btn = QPushButton("Check Source for Raw Packets...")
        check_btn.clicked.connect(
            lambda: self._check_raw(self.fields["source"].get(), interactive=True))
        self.form.addWidget(check_btn)

        self.add_hint(
            "Both backends auto-detect the type from what you give it. Use 'File...' "
            "for a .pcap, .osf, or ROS1 .bag - use 'Folder...' for a ROS2 bag (a folder "
            "containing .db3 + metadata.yaml). Picking a ROS2 bag folder automatically "
            "checks it for raw Ouster packets and offers to convert it. ROS bag reading "
            "needs the 'rosbags' Python package for either backend.")

        self.add_file_field("meta", "Meta JSON file (optional for rosbag sources):",
                             [("JSON files", "*.json"), ("All files", "*.*")])
        self.add_hint("[Ouster CLI] Required for a .pcap source, optional for rosbag "
                       "(resolved from the bag if left blank). [KISS-ICP] Only used with "
                       "the 'ouster' dataloader (.pcap sources).")

        output_default = self.resolve_project_output_default(pipeline, "slam", ".ply")
        self.add_save_field("output", "Output .ply:", default_ext=".ply", default=output_default)
        self.register_auto_default(
            "output", lambda p: self.resolve_project_output_default(p, "slam", ".ply"))

        self.add_text_field("map_max_range", "Map max range (m, optional):")
        self.add_hint(
            "Optional. Use this value to remove far noise, for example in open areas. "
            "The saved map does not include points that are farther than this distance "
            "from the sensor. SLAM registration still uses the full sensor range. Set a "
            "value a little larger than the distance from the robot path to the farthest "
            "surface that you must keep. Leave blank to keep all points. Applies to both "
            "backends.")

        # Only the section for the selected Backend is visible - mirrors
        # begin_section()/end_section() + grid_remove()/grid() in the
        # Tkinter version, using show()/hide() instead.
        self.ouster_section = self.begin_section()
        self.add_preset_selector("Voxel size preset:", [
            ("Fine (15 cm) - slower, most detail", {"voxel_size": "0.15"}),
            ("Medium (25 cm) - balanced, good default", {"voxel_size": "0.25"}),
            ("Coarse (50 cm) - fastest, least detail", {"voxel_size": "0.5"}),
        ])
        # Preset values stay in METRES - LengthFieldRef.set() shows them in cm.
        self.add_length_field("voxel_size", "Voxel size (cm):", default_m="0.25",
                              min_cm=1, max_cm=300)
        self.add_hint("Smaller = more detail but slower and larger output files. Start "
                       "with Medium unless you have a specific reason to change it.")
        self.add_checkbox("visualize", "Open visualizer after processing")
        self.end_section()

        self.kiss_section = self.begin_section()
        self.add_file_field("kiss_icp_script", "Script (.py):", [("Python files", "*.py")],
                             default=str(SCRIPTS_DIR / "slam_kiss_icp.py"))
        bundled_config = CONFIGS_DIR / "kiss_icp_config_indoor.yaml"
        config_edit = self.add_file_field(
            "kiss_icp_config", "Config .yaml (recommended):",
            [("YAML files", "*.yaml *.yml"), ("All files", "*.*")],
            default=str(bundled_config) if bundled_config.exists() else "")
        self.add_hint("STRONGLY recommended for indoor/compartment use. Without a config, "
                       "kiss-icp's defaults assume vehicle-scale outdoor odometry - "
                       "confirmed to under-populate an indoor map by roughly 190x on a "
                       "real capture. Defaults to the bundled kiss_icp_config_indoor.yaml "
                       "if it sits next to this script.")

        self.kiss_voxel_size_label = QLabel()
        self.kiss_voxel_size_label.setWordWrap(True)
        self.form.addWidget(self.kiss_voxel_size_label)
        self.add_length_field("kiss_icp_voxel_size", "Voxel size override (cm, optional):",
                              min_cm=0.5, max_cm=100)
        self.add_hint("Leave blank to use the config's own voxel_size, or kiss-icp's "
                       "built-in default if no config is given.")

        self.kiss_min_range_label = QLabel()
        self.kiss_min_range_label.setWordWrap(True)
        self.form.addWidget(self.kiss_min_range_label)
        self.add_text_field("kiss_icp_min_range", "Min range override (m, optional):")
        self.add_hint("Crops points closer than this before SLAM runs. Useful when the "
                       "sensor sees part of its own mount hardware - shows up as a dense, "
                       "unmoving blob that can confuse the solver. Leave blank to use the "
                       "config's own value, or 0.0/no cropping if the config doesn't set "
                       "one either. Start small (0.2-0.3m) and raise only as needed.")

        self.kiss_max_range_label = QLabel()
        self.kiss_max_range_label.setWordWrap(True)
        self.form.addWidget(self.kiss_max_range_label)
        self.add_hint(
            "Do not decrease max_range in the config to remove far noise. In KISS-ICP, "
            "max_range also removes map points that are far from the robot, so the "
            "saved map keeps only the area near the last robot position. Use 'Map max "
            "range' above, or the horizontal and vertical limits below.")

        self.add_text_field("kiss_icp_map_max_horizontal",
                            "Map max horizontal distance (m, optional):")
        self.add_text_field("kiss_icp_map_max_vertical",
                            "Map max vertical distance (m, optional):")
        self.add_hint(
            "Optional. KISS-ICP only. The saved map does not include points that are "
            "farther than these distances from the sensor. The horizontal distance is "
            "along the floor. The vertical distance is up or down. Use a horizontal limit "
            "to remove far noise in an open area and keep a high ceiling. Use a vertical "
            "limit to remove points far above or below the sensor. 'Vertical' is the up "
            "direction of the sensor at the start of the scan. You can use these limits "
            "together with 'Map max range'. A point must be in all of the limits that you "
            "set.")

        self.add_text_field("kiss_icp_dataloader", "Force dataloader (optional):")
        self.add_text_field("kiss_icp_topic", "Rosbag topic (optional):")
        self.add_hint("Dataloader: leave blank to auto-detect from the source. Topic: "
                       "only needed if a rosbag has more than one PointCloud2 topic.")
        self.end_section()

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
            cmd = core.build_kiss_icp_slam_command(
                script, source, output, config=config, dataloader=dataloader,
                topic=topic, meta=meta, voxel_size=kiss_voxel_size, min_range=kiss_min_range,
                map_max_range=map_max_range, map_max_horizontal=map_max_horizontal,
                map_max_vertical=map_max_vertical, pipeline=active_pipeline)

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
               "Map max range with the Ouster CLI backend (ouster-cli 'clip' after "
               "'slam') is not yet checked on real data. Look at the map in CloudCompare "
               "and make sure that the far points are gone.\n\n"
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
