#!/usr/bin/env python3
"""
Test for the Stage 1 (SLAM) QBot odometry fields (Update 30).

What it checks:
  1. The four fields exist in the Advanced block of the KISS-ICP section: save poses
     (off), save odometry path (off), odometry prior (off), sensor mount yaw (blank).
  2. All off: the command and the report are the same as before update 30, and the
     call of build_kiss_icp_slam_command has none of the new arguments.
  3. Save poses on: --save-poses <map name>.poses.txt, and a report line.
  4. Prior on: --odometry-prior qbot. A mount yaw: --odometry-mount-yaw.
  5. A mount yaw that is not a number: an error that names the field, and the Advanced
     block opens so that the field is visible.
  6. The Advanced header counts the changed fields.
  7. The Ouster CLI backend ignores the fields.
  8. The Hesai workflow of updates 22 and 23 (Sensor preset, flying-pixel filter) works
     together with the new fields.
  9. The record of a project run: the new keys are there only when an option is on.
 10. build_kiss_icp_slam_command: an old call gives the same command as before, the
     new arguments are checked.

  11. The "?" help buttons of update 29: each new field has one at the end of its row, with
     the same rules as the other fields (the popup starts with 'What it does:' and has
     'Change it when', no banned word, under 1100 characters, no sentence of more than 25
     words), and the page has no new grey hint.

Run (from the gui/ folder):
    python test_stage1_odometry_qt.py
"""
import os
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

recorded = []
stand_in = types.ModuleType("project_manager")
stand_in.ProjectError = type("ProjectError", (Exception,), {})
stand_in.start_stage = lambda pipeline, stage, params=None: recorded.append((stage, params))
sys.modules["project_manager"] = stand_in

from PySide6.QtWidgets import QApplication              # noqa: E402

import pipeline_core as core                              # noqa: E402
import stage1_slam_dialog_qt as s1                        # noqa: E402

app = QApplication.instance() or QApplication([])
PASSED = 0


def check(label, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {label}")
    else:
        print(f"  [FAIL] {label} {detail}")
        raise AssertionError(label)


tmp = Path(tempfile.mkdtemp())
bag = tmp / "bag"
bag.mkdir()
(bag / "metadata.yaml").write_text(
    "rosbag2_bagfile_information:\n  topics_with_message_count:\n"
    "    - topic_metadata:\n        name: /lidar_points\n"
    "        type: sensor_msgs/msg/PointCloud2\n      message_count: 10\n", encoding="utf-8")
pcap = tmp / "scan.pcap"
pcap.write_bytes(b"x")
OUT = tmp / "map_001.ply"

dlg = s1.Stage1SlamDialog(pipeline=None)
fields = dlg.fields
fields["output"].set(str(OUT))
fields["source"].set(str(bag))
fields["backend"].set("kiss_icp")

NEW_OPTIONS = ("--save-poses", "--save-odometry", "--odometry-prior", "--odometry-mount-yaw")

print("\n=== 1. The fields ===")
for key in ("kiss_icp_save_poses", "kiss_icp_save_odometry", "kiss_icp_odometry_prior",
            "kiss_icp_odometry_mount_yaw"):
    check(f"field {key} exists", key in fields)
    check(f"field {key} is in the KISS-ICP Advanced block",
          key in dlg.kiss_advanced["keys"], str(dlg.kiss_advanced["keys"]))
check("save poses is off by default", fields["kiss_icp_save_poses"].get() is False)
check("save odometry is off by default", fields["kiss_icp_save_odometry"].get() is False)
check("the odometry prior is off by default", fields["kiss_icp_odometry_prior"].get() == "off")
check("the mount yaw is blank by default", fields["kiss_icp_odometry_mount_yaw"].get() == "")
check("the Advanced block is closed when the page opens", not dlg.kiss_advanced["body"].isVisibleTo(dlg))
check("no field counts as changed at the start", dlg.advanced_changed_count(dlg.kiss_advanced) == 0)

print("\n=== 2. All off ===")
recorded_calls = []
real_builder = core.build_kiss_icp_slam_command


def spy(*args, **kwargs):
    recorded_calls.append(kwargs)
    return real_builder(*args, **kwargs)


core.build_kiss_icp_slam_command = spy
cmd, report, _info = dlg._build_run()
core.build_kiss_icp_slam_command = real_builder
check("no new option in the command", all(o not in cmd for o in NEW_OPTIONS), str(cmd))
check("the call has none of the new arguments",
      recorded_calls and all(k not in recorded_calls[-1]
                             for k in ("save_poses", "save_odometry", "odometry_prior",
                                "odometry_mount_yaw")),
      str(recorded_calls))
check("no new line in the report",
      "Poses file" not in report and "Odometry file" not in report
      and "QBot odometry prior" not in report and "Sensor mount yaw" not in report)
base_cmd = core.build_kiss_icp_slam_command(
    fields["kiss_icp_script"].get(), str(bag), str(OUT),
    config=fields["kiss_icp_config"].get().strip() or None)
check("the command equals the command of an old call", cmd == base_cmd, f"{cmd}\n{base_cmd}")

print("\n=== 3. Save poses ===")
fields["kiss_icp_save_poses"].set(True)
cmd, report, _info = dlg._build_run()
poses = str(tmp / "map_001.poses.txt")
check("--save-poses with the path of the map and '.poses.txt'",
      "--save-poses" in cmd and cmd[cmd.index("--save-poses") + 1] == poses, str(cmd))
check("the report names the poses file", f"Poses file: {poses}" in report, report)
check("the rest of the command is as before",
      cmd[:len(base_cmd)] == base_cmd and cmd[len(base_cmd):] == ["--save-poses", poses], str(cmd))
check("the Advanced header counts one changed field",
      "1 changed from default" in dlg.kiss_advanced["header"].text(),
      dlg.kiss_advanced["header"].text())

print("\n=== 3b. Save odometry ===")
fields["kiss_icp_save_poses"].set(False)
fields["kiss_icp_save_odometry"].set(True)
cmd, report, _info = dlg._build_run()
odo = str(tmp / "map_001.odometry.txt")
check("--save-odometry with the path of the map and '.odometry.txt'",
      cmd[len(base_cmd):] == ["--save-odometry", odo], str(cmd))
check("the report names the odometry file", f"Odometry file: {odo}" in report, report)
check("the Advanced header counts one changed field",
      "1 changed from default" in dlg.kiss_advanced["header"].text(), dlg.kiss_advanced["header"].text())
fields["kiss_icp_save_poses"].set(True)
cmd, report, _info = dlg._build_run()
check("poses and odometry together: both options, poses first",
      cmd[len(base_cmd):] == ["--save-poses", str(tmp / "map_001.poses.txt"), "--save-odometry", odo], str(cmd))
fields["kiss_icp_save_poses"].set(False)
fields["kiss_icp_save_odometry"].set(False)

print("\n=== 4. Prior and mount yaw ===")
fields["kiss_icp_save_poses"].set(False)
fields["kiss_icp_odometry_prior"].set("qbot")
cmd, report, _info = dlg._build_run()
check("--odometry-prior qbot", cmd[len(base_cmd):] == ["--odometry-prior", "qbot"], str(cmd))
check("the report says that the prior is on", "QBot odometry prior: on" in report, report)
check("no mount yaw option when the field is blank", "--odometry-mount-yaw" not in cmd)
fields["kiss_icp_odometry_mount_yaw"].set("-90")
cmd, report, _info = dlg._build_run()
check("--odometry-mount-yaw -90.0",
      "--odometry-mount-yaw" in cmd and cmd[cmd.index("--odometry-mount-yaw") + 1] == "-90.0", str(cmd))
check("the report names the mount yaw", "Sensor mount yaw for the odometry: -90.0 degrees" in report)
fields["kiss_icp_odometry_prior"].set("off")
cmd, report, _info = dlg._build_run()
check("a mount yaw alone (prior off) is passed: the check of the script uses it",
      cmd[len(base_cmd):] == ["--odometry-mount-yaw", "-90.0"], str(cmd))

print("\n=== 5. A mount yaw that is not a number ===")
fields["kiss_icp_odometry_mount_yaw"].set("ninety")
try:
    dlg._build_run()
    message = None
except ValueError as error:
    message = str(error)
check("ValueError", message is not None)
check("the message names the field", "Sensor mount yaw (degrees, optional)" in (message or ""), message)
dlg.kiss_advanced["header"].setChecked(False)
dlg._reveal_advanced_for_error(message)
check("the Advanced block opens", dlg.kiss_advanced["header"].isChecked())
fields["kiss_icp_odometry_mount_yaw"].set("")

print("\n=== 6. The Advanced header ===")
check("back to the defaults: no changed field", dlg.advanced_changed_count(dlg.kiss_advanced) == 0,
      dlg.kiss_advanced["header"].text())

print("\n=== 7. The Ouster CLI backend ignores the fields ===")
fields["kiss_icp_save_poses"].set(True)
fields["kiss_icp_save_odometry"].set(True)
fields["kiss_icp_odometry_prior"].set("qbot")
fields["kiss_icp_odometry_mount_yaw"].set("-90")
fields["backend"].set("ouster")
fields["source"].set(str(pcap))
cmd, report, _info = dlg._build_run()
check("no new option in the Ouster CLI command", all(o not in cmd for o in NEW_OPTIONS), str(cmd))
check("no new line in the Ouster CLI report",
      "Poses file" not in report and "Odometry file" not in report
      and "QBot odometry prior" not in report)

print("\n=== 8. Updates 22 and 23 together with the new fields ===")
fields["source"].set(str(bag))
fields["backend"].set("kiss_icp")
fields["kiss_icp_flying_pixel_filter"].set(True)
combo = dlg.sensor_preset_combo
items = [combo.itemText(i) for i in range(combo.count())]
combo.setCurrentIndex(next(i for i, t in enumerate(items) if t.startswith("Hesai")))
cmd, report, _info = dlg._build_run()
check("Hesai config, flying-pixel filter, poses, prior and mount yaw in one command",
      any("kiss_icp_config_hesai.yaml" in str(c) for c in cmd)
      and "--flying-pixel-filter" in cmd and "--save-poses" in cmd and "--save-odometry" in cmd
      and "--odometry-prior" in cmd and "--odometry-mount-yaw" in cmd, str(cmd))
check("the report has both lines",
      "Flying-pixel filter: on" in report and "Poses file:" in report, report)
fields["kiss_icp_flying_pixel_filter"].set(False)
fields["kiss_icp_save_poses"].set(False)
fields["kiss_icp_save_odometry"].set(False)
fields["kiss_icp_odometry_prior"].set("off")
fields["kiss_icp_odometry_mount_yaw"].set("")


class Pipeline:                      # stand-in for a project_manager.PipelineHandle
    project = object()


print("\n=== 9. The record of a project run ===")
recorded.clear()
core.build_kiss_icp_slam_command("s.py", "in.bag", "out.ply", pipeline=Pipeline())
check("all options off: none of the new keys is recorded",
      all(k not in recorded[-1][1] for k in
          ("save_poses", "poses_file", "save_odometry", "odometry_file", "odometry_prior",
           "odometry_mount_yaw")), str(recorded))
recorded.clear()
core.build_kiss_icp_slam_command("s.py", "in.bag", "out.ply", pipeline=Pipeline(),
                                 save_poses=True, save_odometry=True, odometry_prior="qbot",
                                 odometry_mount_yaw=-90)
params = recorded[-1][1]
check("options on: the keys are recorded",
      params.get("save_poses") is True and params.get("odometry_prior") == "qbot"
      and params.get("odometry_mount_yaw") == -90.0, str(params))
check("the poses file is recorded (a full path when the project cannot make it relative)",
      params.get("poses_file") == str(Path("out.poses.txt")), str(params))
check("the odometry file is recorded the same way",
      params.get("save_odometry") is True and params.get("odometry_file") == str(Path("out.odometry.txt")),
      str(params))
stand_in.to_relative_path = lambda project, path: "slam/" + Path(path).name
recorded.clear()
core.build_kiss_icp_slam_command("s.py", "in.bag", "x/out.ply", pipeline=Pipeline(),
                                 save_poses=True)
check("the poses file is recorded as a project path when the project can make one",
      recorded[-1][1].get("poses_file") == "slam/out.poses.txt", str(recorded))
del stand_in.to_relative_path

print("\n=== 10. build_kiss_icp_slam_command ===")
old = core.build_kiss_icp_slam_command("s.py", "in.bag", "out.ply", config="c.yaml",
                                       voxel_size=0.05, map_max_range=8.0)
check("an old call gives the command of before",
      old == [sys.executable, "s.py", "--input", "in.bag", "--output", "out.ply",
              "--config", "c.yaml", "--voxel-size", "0.05", "--map-max-range", "8.0"], str(old))
check("the helper gives the poses path",
      core.kiss_icp_poses_path("a/b/map_001.ply") == Path("a/b/map_001.poses.txt"))
check("the helper gives the odometry path",
      core.kiss_icp_odometry_path("a/b/map_001.ply") == Path("a/b/map_001.odometry.txt"))
old_call = core.build_kiss_icp_slam_command("s.py", "in.bag", "out.ply", save_odometry=True)
check("save_odometry=True adds --save-odometry <map>.odometry.txt at the end",
      old_call[-2:] == ["--save-odometry", str(Path("out.odometry.txt"))], str(old_call))
for bad_prior in ("Qbot", "on", ""):
    try:
        core.build_kiss_icp_slam_command("s.py", "in.bag", "out.ply", odometry_prior=bad_prior)
        refused = False
    except ValueError:
        refused = True
    check(f"odometry_prior={bad_prior!r} is refused", refused)
try:
    core.build_kiss_icp_slam_command("s.py", "in.bag", "out.ply", odometry_mount_yaw="abc")
    refused = False
except ValueError:
    refused = True
check("a mount yaw that is not a number is refused", refused)
check("a mount yaw as text with a number is accepted",
      "--odometry-mount-yaw" in core.build_kiss_icp_slam_command(
          "s.py", "in.bag", "out.ply", odometry_mount_yaw="-90"))

print("\n=== 11. The '?' help buttons (update 29 pattern) ===")
import html as _html
import re
import qt_stage_base as base
from PySide6.QtWidgets import QLabel

BANNED = re.compile(r"test data|real data|real project|simulated|\bsim\d?\b|tested on|"
                    r"confirmed|in a test|on the test|Update \d+|\bwe found\b", re.I)
page = s1.Stage1SlamDialog(pipeline=None)
page_buttons = page._help_buttons
new_keys = ("kiss_icp_save_poses", "kiss_icp_save_odometry", "kiss_icp_odometry_prior",
            "kiss_icp_odometry_mount_yaw")


def last_widget_of_row(key):
    ref = page.fields[key]
    if key in getattr(page, "_help_rows", {}):
        row = page._help_rows[key]
    else:
        widget = getattr(ref, "widget", None)
        row = base._find_row_layout(page.form_root_layout(), widget) if widget is not None else None
    return row.itemAt(row.count() - 1).widget() if row is not None else None


for key in new_keys:
    button = last_widget_of_row(key)
    check(f"{key}: a '?' button is at the end of the row", isinstance(button, base.HelpButton))
    text = _html.unescape(button.help_html)
    plain = re.sub("<[^>]+>", "", text)
    check(f"{key}: the popup starts with 'What it does:' and has 'Change it when'",
          button.help_html.startswith("<b>What it does:</b>") and "Change it when" in button.help_html)
    check(f"{key}: no banned word in the popup", not BANNED.search(text), BANNED.findall(text))
    check(f"{key}: the popup is shorter than 1100 characters", len(plain) < 1100, len(plain))
    lines = re.sub("<[^>]+>", "", _html.unescape(button.help_html.replace("<br>", "\n")))
    long_sentences = [s[:40] for s in re.split(r"(?<=[.!?])\s+|\n", lines)
                      if len(s.split()) > 25]
    check(f"{key}: no sentence has more than 25 words", not long_sentences, long_sentences)
without = [key for key in page.fields if not isinstance(last_widget_of_row(key), base.HelpButton)]
check("every field of the Stage 1 page has a '?' button at the end of its row", not without, without)
check("the popups of the page are all in the right form",
      all(b.help_html.startswith("<b>What it does:</b>") and "Change it when" in b.help_html
          for b in page_buttons))
hints = [w.text() for w in page.findChildren(QLabel)
         if "#777777" in w.styleSheet() and "font-size: 8pt" in w.styleSheet()]
check("the visible grey hints of the page are shorter than 600 characters in total",
      sum(len(h) for h in hints) < 600, sum(len(h) for h in hints))
check("none of the new fields has a grey hint (the text is in the popup)",
      not any("QBot" in h or "poses" in h.lower() for h in hints), hints)
check("no hint or popup of the page names an update or test data",
      not [x[:50] for x in hints + [_html.unescape(b.help_html) for b in page_buttons]
           if BANNED.search(x)])
page.close()

print(f"\nAll {PASSED} checks passed.")
