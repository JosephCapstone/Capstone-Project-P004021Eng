#!/usr/bin/env python3
"""
Offscreen regression tests for the Qt app (no window is shown).

Each section reproduces a bug that was found before the first team test
build, and checks that the fix holds. Runs with no LiDAR data, no
CloudCompare, and no ouster-cli: stage runs use small fake scripts.

Run (from the gui/ folder):
    python test_qt_smoke.py

On Windows, the QT_QPA_PLATFORM line below is optional - a real window
system is available there. It is set so the test also runs headless.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

PASSED = 0
DIALOGS = []


def check(label, condition):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [PASS] {label}")
    else:
        print(f"  [FAIL] {label}")
        raise AssertionError(label)


# Record message boxes instead of blocking on them. question() answers
# QUESTION_ANSWER[0] (Yes by default).
QUESTION_ANSWER = [QMessageBox.Yes]


def _record(kind):
    def fn(*args, **kwargs):
        DIALOGS.append((kind, args[1] if len(args) > 1 else "", args[2] if len(args) > 2 else ""))
        return QUESTION_ANSWER[0] if kind == "question" else QMessageBox.Yes
    return staticmethod(fn)


for _kind in ("information", "critical", "warning", "question"):
    setattr(QMessageBox, _kind, _record(_kind))

app = QApplication(sys.argv)

import pipeline_applet_qt_template as main_window  # noqa: E402
import pipeline_core as core  # noqa: E402
import project_manager as pm  # noqa: E402
import qt_stage_base  # noqa: E402

# Success popups (green tick) go through one helper - record them too.
qt_stage_base.show_success_message = (
    lambda parent, title, text: DIALOGS.append(("success", title, text)))


def pump(condition, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    app.processEvents()
    return condition()


def new_project(tmp):
    raw = tmp / "capture.pcap"
    raw.write_bytes(b"fake pcap")
    root = tmp / "projects"
    root.mkdir(exist_ok=True)
    return pm.create_project(root, "comp_01", raw, "pcap")


def fake_script(tmp, name, body):
    path = tmp / name
    path.write_text(body, encoding="utf-8")
    return path


# A Stage 2 stand-in: writes whatever --output names, optionally slowly.
LEVEL_OK = (
    "import sys, time\n"
    "a = sys.argv\n"
    "out = a[a.index('--output') + 1]\n"
    "open(out, 'w').write('ply')\n"
    "print('fake level ok', flush=True)\n"
)
LEVEL_SLOW = (
    "import time\n"
    "for i in range(600):\n"
    "    print('working', i, flush=True)\n"
    "    time.sleep(0.1)\n"
)

tmp = Path(tempfile.mkdtemp())
win = main_window.PipelineAppletWindow()

print("\n=== Startup ===")
check("all eight stage panels import", all(main_window.STAGE_PANEL_CLASSES.values()))
check("no panel import errors", not main_window.PANEL_IMPORT_ERRORS)

print("\n=== Item 8: a failed panel import is recorded, not hidden ===")
cls = main_window._load_panel_class("Stage X: Test", "no_such_module_xyz", "Nothing")
check("failed load returns None", cls is None)
check("traceback recorded", "no_such_module_xyz" in main_window.PANEL_IMPORT_ERRORS.get(
    "Stage X: Test", ""))
del main_window.PANEL_IMPORT_ERRORS["Stage X: Test"]

project = new_project(tmp)
win._set_active_project(project)
for _button, name in main_window.STAGE_BUTTONS:
    win._show_stage(name)

print("\n=== Item 2: output path follows the active pipeline ===")
p1 = win._stage_pages["Stage 1: SLAM"]
baseline_out = p1.fields["output"].get()
check("Stage 1 output starts in baseline/01_slam", "baseline" in Path(baseline_out).parts)
raw2 = tmp / "capture2.pcap"
raw2.write_bytes(b"fake pcap 2")
scan_id = pm.add_scan(project, "post", raw2, "pcap")
win._on_scan_added(scan_id)
scan_out = p1.fields["output"].get()
check("after switch to the scan, output is in the scan folder",
      Path(scan_out).resolve().is_relative_to(project.scan_handle(scan_id).root.resolve()))
win.ui.sourceCombo.setCurrentText("Baseline")
check("switch back to Baseline restores the baseline path",
      p1.fields["output"].get() == baseline_out)

p1.fields["output"].set(str(tmp / "my_own_choice.ply"))
win.ui.sourceCombo.setCurrentText(f"Scan: {scan_id}")
check("a user-typed output path is not replaced on a switch",
      p1.fields["output"].get() == str(tmp / "my_own_choice.ply"))
win.ui.sourceCombo.setCurrentText("Baseline")

print("\n=== Item 1: output outside the pipeline folder is blocked BEFORE start_stage ===")
p2 = win._stage_pages["Stage 2: Level"]
inp = tmp / "in.ply"
inp.write_text("ply")
p2.fields["script"].set(str(fake_script(tmp, "level_ok.py", LEVEL_OK)))
p2.fields["input"].set(str(inp))
p2.fields["output"].set(str(tmp / "Desktop_out.ply"))
DIALOGS.clear()
p2._on_run_clicked()
check("run refused with an output-path message",
      DIALOGS and DIALOGS[-1][1] == qt_stage_base.RunCheckError.title)
check("stage never marked running", project.baseline_handle().entry["stages"]["level"]
      .get("status") == "not_started")
check("app not locked", not win._is_running)

print("\n=== Item 1: a run in ANOTHER pipeline's folder is blocked too ===")
scan_level_out = pm.get_absolute_path(project, pm.get_output_path(
    project.scan_handle(scan_id), "level", ".ply"))
p2.fields["output"].set(str(scan_level_out))
DIALOGS.clear()
p2._on_run_clicked()
check("run refused", DIALOGS and DIALOGS[-1][1] == qt_stage_base.RunCheckError.title)

print("\n=== Item 2: empty output field is filled at Run time; success advances the number ===")
p2.fields["output"].set("")
DIALOGS.clear()
p2._on_run_clicked()
check("run started", win._is_running)
check("finished", pump(lambda: not win._is_running))
level = project.baseline_handle().entry["stages"]["level"]
check("stage recorded complete", level.get("status") == "complete")
check("recorded output is _001", level.get("output", "").endswith("_level_001.ply"))
check("output field moved on to _002", p2.fields["output"].get().endswith("_level_002.ply"))
check("Stage Report shown with the success (tick) popup",
      any(k == "success" and t == "Stage Report" for k, t, _m in DIALOGS))
check("the tick icon draws", not qt_stage_base._tick_pixmap(48, 1.0).isNull())

print("\n=== Item 1: a project-record failure still releases the lock ===")
real_finish = core.finish_stage


def broken_finish(*args, **kwargs):
    raise pm.ProjectError("simulated record failure")


core.finish_stage = broken_finish
try:
    DIALOGS.clear()
    p2._on_run_clicked()
    check("finished", pump(lambda: not win._is_running))
    check("status line shows an error, not Running",
          win.ui.runStatusLabel.text().startswith("Error"))
    check("Run buttons enabled again", p2.run_button.isEnabled()
          and win._stage_pages["Stage 3: Cleanup"].run_button.isEnabled())
    check("user told about the record failure",
          any(kind == "warning" and title == "Project record failed"
              for kind, title, _ in DIALOGS))
finally:
    core.finish_stage = real_finish

print("\n=== Item 6: Stop ends the whole process and records a failure ===")
p2.fields["script"].set(str(fake_script(tmp, "level_slow.py", LEVEL_SLOW)))
p2.fields["output"].set("")
p2._on_run_clicked()
check("running", win._is_running)
check("Stop enabled while running", p2.stop_button.isEnabled())
check("other panels locked", not win._stage_pages["Stage 5: Diff"].run_button.isEnabled())
check("process handle arrives", pump(lambda: p2._active_run and p2._active_run["process"]))
proc = p2._active_run["process"]
p2.request_stop()
check("run ends after Stop", pump(lambda: not win._is_running, timeout=10))
check("process is gone", proc.poll() is not None)
level = project.baseline_handle().entry["stages"]["level"]
check("stage recorded failed", level.get("status") == "failed")
check("reason says stopped by user", "Stopped by user" in str(level))
check("Stop disabled again", not p2.stop_button.isEnabled())

print("\n=== Item 6: closing the app during a run stops it and records it ===")
p2._on_run_clicked()
check("process handle arrives", pump(lambda: p2._active_run and p2._active_run["process"]))
proc = p2._active_run["process"]
p2.abort_for_shutdown()
check("process stopped on shutdown", pump(lambda: proc.poll() is not None, timeout=10))
# Let the late process-exit callback run first: it must NOT overwrite
# the shutdown record (this was a real race, found by this test).
pump(lambda: not win._is_running)
check("stage recorded failed on shutdown", "app was closed" in str(
    project.baseline_handle().entry["stages"]["level"]))

print("\n=== Item 7: a helper process uses the same lock ===")
p1 = win._stage_pages["Stage 1: SLAM"]
started = p1._run_utility_command(
    [sys.executable, str(fake_script(tmp, "slow_helper.py", LEVEL_SLOW))],
    lambda code, cancelled: None)
check("helper started", started)
check("helper engages the cross-panel lock", win._is_running
      and not win._stage_pages["Stage 2: Level"].run_button.isEnabled())
DIALOGS.clear()
second = win._stage_pages["Stage 8: Export"]._run_utility_command(
    [sys.executable, "-c", "print(1)"], lambda code, cancelled: None)
check("a second helper in another panel is refused", second is False)
check("helper can be stopped", pump(lambda: p1._active_run and p1._active_run["process"]))
p1.request_stop()
check("helper ends", pump(lambda: not win._is_running, timeout=10))

print("\n=== Map max range reaches both SLAM commands ===")
p1.fields["source"].set(str(inp))
p1.fields["output"].set("")
p1.fields["map_max_range"].set("8")
p1.fields["backend"].set("kiss_icp")
p1._refresh_auto_defaults(only_empty=True)
cmd, _report, _info = p1._build_run()
check("KISS-ICP cmd has --map-max-range 8.0", cmd[cmd.index("--map-max-range") + 1] == "8.0")
check("recorded in project params", project.baseline_handle().entry["stages"]["slam"]
      ["params"]["map_max_range"] == 8.0)
p1.fields["backend"].set("ouster")
cmd, _report, _info = p1._build_run()
check("Ouster CLI cmd clips RANGE at 8000 mm after slam, before save",
      cmd.index("slam") < cmd.index("clip") < cmd.index("save")
      and cmd[cmd.index("clip") + 1:cmd.index("clip") + 3] == ["RANGE", ":8000mm"])
p1.fields["map_max_range"].set("-2")
DIALOGS.clear()
p1._on_run_clicked()
check("a negative value is refused", DIALOGS and "greater than 0" in DIALOGS[-1][2])

print("\n=== Horizontal / vertical map limits (KISS-ICP only) ===")
p1.fields["backend"].set("kiss_icp")
p1.fields["map_max_range"].set("")
p1.fields["kiss_icp_map_max_horizontal"].set("8")
p1.fields["kiss_icp_map_max_vertical"].set("2.5")
p1.fields["output"].set("")
p1._refresh_auto_defaults(only_empty=True)
cmd, report, _info = p1._build_run()
check("--map-max-horizontal 8.0 on the KISS-ICP command",
      cmd[cmd.index("--map-max-horizontal") + 1] == "8.0")
check("--map-max-vertical 2.5 on the KISS-ICP command",
      cmd[cmd.index("--map-max-vertical") + 1] == "2.5")
slam_params = project.baseline_handle().entry["stages"]["slam"]["params"]
check("both recorded in project params",
      slam_params["map_max_horizontal"] == 8.0 and slam_params["map_max_vertical"] == 2.5)
check("report names both limits", "horizontal distance: 8.0 m" in report
      and "vertical distance: 2.5 m" in report)
p1.fields["kiss_icp_map_max_vertical"].set("0")
DIALOGS.clear()
p1._on_run_clicked()
check("a zero vertical limit is refused", DIALOGS and "greater than 0" in DIALOGS[-1][2])
p1.fields["backend"].set("ouster")
p1.fields["output"].set("")
p1._refresh_auto_defaults(only_empty=True)
cmd, _report, _info = p1._build_run()
check("Ouster CLI ignores the KISS-only limits", "clip" not in cmd
      and "--map-max-horizontal" not in cmd)
p1.fields["kiss_icp_map_max_horizontal"].set("")
p1.fields["kiss_icp_map_max_vertical"].set("")

print("\n=== Item 5: Extract Damage Detail radius pre-fill ===")
params_file = tmp / "m3c2_params.txt"
core.generate_m3c2_params_file(params_file, 0.0425, 0.02125, 0.085, 0.004)
check("read_m3c2_normal_scale reads the file", core.read_m3c2_normal_scale(params_file) == 0.0425)
diff_id = pm.add_diff(project, "d1", "baseline", scan_id)
diff = project.diff_handle(diff_id)
core.build_diff_command("a.ply", "b.ply", params_file, pipeline=diff)
check("build_diff_command records params.normal_scale",
      diff.entry["stages"]["diff"]["params"]["normal_scale"] == 0.0425)
win._on_diff_added(diff_id)
p8 = win._stage_pages["Stage 8: Export"]
check("radius pre-filled from the recorded normal scale, shown in cm",
      p8.fields["damage_detail_radius"].widget.text() == "4.25")
check("radius field still returns metres", p8.fields["damage_detail_radius"].get() == "0.0425")

print("\n=== Item 5: Extract Damage Detail runs for real ===")
flagged = tmp / "flagged.ply"
flagged.write_text("ply")
comparison = tmp / "comparison.ply"
comparison.write_text("ply")
p8.fields["damage_detail_flagged"].set(str(flagged))
p8.fields["damage_detail_comparison"].set(str(comparison))
fake_detail = fake_script(tmp, "extract_damage_detail.py", LEVEL_OK)
real_scripts_dir = qt_stage_base.SCRIPTS_DIR
import stage8_export_dialog_qt as stage8  # noqa: E402
stage8.SCRIPTS_DIR = tmp
try:
    DIALOGS.clear()
    p8._extract_damage_detail()
    check("extract started with the lock", win._is_running)
    check("extract finished", pump(lambda: not win._is_running))
finally:
    stage8.SCRIPTS_DIR = real_scripts_dir
detail = p8.fields["detail"].get()
check("detail field filled", detail.endswith(".ply") and Path(detail).exists())
check("extract success shown with the tick popup",
      any(k == "success" and t == "Damage detail extracted" for k, t, _m in DIALOGS))
check("detail saved in the diff's export folder",
      Path(detail).resolve().is_relative_to(diff.root.resolve()))

print("\n=== Item 9: New Project copies on a worker thread ===")
from project_workflow_qt import NewProjectDialog  # noqa: E402
created = []
dlg = NewProjectDialog(win, on_created=created.append)
dlg.location_edit.setText(str(tmp / "projects"))
dlg.compartment_edit.setText("comp_02")
dlg.source_edit.setText(str(raw2))
dlg._create()
check("project created via the worker", pump(lambda: bool(created)))
check("created project is loadable", pm.load_project(created[0].root) is not None)

DIALOGS.clear()
failed_dlg = NewProjectDialog(win, on_created=created.append)
failed_dlg.location_edit.setText(str(tmp / "projects"))
failed_dlg.compartment_edit.setText("comp_02")  # same name and date - folder exists
failed_dlg.source_edit.setText(str(raw2))
failed_dlg._create()
check("failure reported", pump(lambda: any(k == "critical" for k, _t, _m in DIALOGS)))

print("\n=== Centimetre fields: display cm, return metres ===")
ref = win._stage_pages["Stage 4: Segment"].fields["distance_threshold"]
check("Stage 4 default 0.05 m shows as 5", ref.widget.text() == "5")
check("get() returns metres", ref.get() == "0.05")
ref.widget.setText("2.5")
check("typed 2.5 cm returns 0.025 m", ref.get() == "0.025")
ref.set("0.05")
check("blank stays blank", qt_stage_base.LengthFieldRef(
    type(ref.widget)()).get() == "")
p5 = win._stage_pages["Stage 5: Diff"]
p5.fields["registration_rms"].set("0.0184")
check("Stage 5 RMS set in metres shows 1.84 cm",
      p5.fields["registration_rms"].widget.text() == "1.84")
p7 = win._stage_pages["Stage 7: Surface"]
p7.fields["ball_radii"].widget.setText("2, 4,8")
check("Stage 7 ball radii '2, 4,8' cm -> '0.02,0.04,0.08' m",
      p7.fields["ball_radii"].get() == "0.02,0.04,0.08")
check("ranges stay in metres: map max range is a plain field",
      not isinstance(p1.fields["map_max_range"], qt_stage_base.LengthFieldRef)
      and not isinstance(p1.fields["kiss_icp_min_range"], qt_stage_base.LengthFieldRef))

p1.fields["backend"].set("ouster")
p1.fields["map_max_range"].set("")
p1.fields["voxel_size"].widget.setText("25")
p1.fields["source"].set(str(inp))
p1.fields["output"].set("")
p1._refresh_auto_defaults(only_empty=True)
cmd, _report, _info = p1._build_run()
check("Stage 1 voxel 25 cm reaches ouster-cli as 0.25",
      cmd[cmd.index("--voxel-size") + 1] == "0.25")
check("project records metres", project.baseline_handle().entry["stages"]["slam"]
      ["params"]["voxel_size"] == 0.25)

print("\n=== A metre value typed into a cm field asks first ===")
p1.fields["voxel_size"].widget.setText("0.25")  # the likely mistake: 0.25 cm
QUESTION_ANSWER[0] = QMessageBox.No
DIALOGS.clear()
p1._on_run_clicked()
check("confirmation asked", any(k == "question" and t == "Check the length values"
                                for k, t, _m in DIALOGS))
check("answer No: nothing runs", not win._is_running)
check("the message names the field and value",
      any("Voxel size (cm): 0.25 cm" in m for _k, _t, m in DIALOGS))
p1.fields["kiss_icp_voxel_size"].widget.setText("0.01")  # hidden: KISS section
p1.fields["voxel_size"].widget.setText("25")
DIALOGS.clear()
check("a hidden section's field is not checked",
      p1._check_length_plausibility() is True and not DIALOGS)
p1.fields["kiss_icp_voxel_size"].widget.setText("")
QUESTION_ANSWER[0] = QMessageBox.Yes

print(f"\nAll {PASSED} checks passed.")
