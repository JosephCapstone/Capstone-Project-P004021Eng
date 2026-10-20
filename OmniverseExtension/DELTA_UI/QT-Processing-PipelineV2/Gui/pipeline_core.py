"""
pipeline_core.py
=================
Shared logic for the SLAM pipeline applet: command builders for each stage
and a threaded subprocess runner that streams output line-by-line to a
callback (so the GUI log console updates live instead of freezing).

Kept separate from the GUI so these functions can also be driven from a
plain script/CLI later without touching Tkinter code.

PROJECT_SCHEMA_v2.md UPDATE: every stage-runner function's optional
project-mode parameter is now called `pipeline` instead of `project`, and
takes a project_manager.PipelineHandle (project.baseline_handle(),
.scan_handle(scan_id), or .diff_handle(diff_id)) instead of a bare
Project. This isn't just a rename - Version 1 had one linear pipeline per
project, so "the project" and "the one pipeline in it" were the same
thing. Version 2 splits a project into a baseline pipeline, zero or more
scan pipelines, and zero or more diff pipelines (PROJECT_SCHEMA_v2.md
Section 4), so a function now needs to know WHICH of those it's reading
from/writing to - that's what the handle carries. Manual mode
(pipeline=None everywhere) is unaffected either way.

One stage genuinely changed shape, not just name: Stage 5 (Diff)'s input
is now a pair (whatever a diff's `reference` and `comparison` point at -
Section 11.3), not a single predecessor's output, because a project can
now hold more than one comparison scan and diff each one against more
than one reference. See build_diff_command()'s docstring.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

# project_manager.py is optional - manual mode (pipeline=None everywhere,
# the only mode that existed before project support) must keep working
# even if this import fails for some reason. Only actually needed when a
# caller passes a real PipelineHandle.
try:
    import project_manager
except ImportError:
    project_manager = None


# ---------------------------------------------------------------------------
# Baseline registry - lets a compartment's baseline be looked up by name
# instead of re-browsing to the file every time, for MANUAL mode only.
# Deliberately small: one JSON file next to the applet, one active
# baseline per compartment plus a history so nothing gets silently
# overwritten.
#
# This is a different system from project.json's own `baseline` object
# (PROJECT_SCHEMA_v2.md Section 3.1): a project's baseline lives and is
# promoted entirely inside that one project's project.json. This registry
# is the still-not-built-yet "compartment registry" Section 3.1 mentions
# as a separate system - kept here, unconnected to project mode, so
# manual mode (no project open at all) still has a way to reuse a
# baseline across runs. promote_baseline() in project_manager.py is a
# stub specifically because wiring it to THIS registry is future work
# (PROJECT_INTEGRATION_PLAN.md Section 6).
# ---------------------------------------------------------------------------

BASELINE_REGISTRY_FILE = Path(__file__).resolve().parent / "baseline_registry.json"


def load_baseline_registry():
    if not BASELINE_REGISTRY_FILE.exists():
        return {}
    try:
        return json.loads(BASELINE_REGISTRY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_baseline_registry(registry):
    BASELINE_REGISTRY_FILE.write_text(json.dumps(registry, indent=2), encoding="utf-8")


def register_baseline(compartment, ply_path, note=""):
    """Sets ply_path as the active baseline for `compartment`, keeping a
    history entry rather than silently discarding the previous one."""
    registry = load_baseline_registry()
    entry = registry.setdefault(compartment, {"active_baseline": None, "history": []})
    entry["history"].append({
        "path": str(ply_path),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": note,
    })
    entry["active_baseline"] = str(ply_path)
    save_baseline_registry(registry)
    return registry


def list_compartments():
    return sorted(load_baseline_registry().keys())


def get_active_baseline(compartment):
    entry = load_baseline_registry().get(compartment)
    return entry["active_baseline"] if entry else None


# ---------------------------------------------------------------------------
# M3C2 params file generation - see generate_m3c2_params.py for the
# standalone CLI version (same template, kept in sync manually since
# standalone scripts in this project intentionally don't import from here).
# ---------------------------------------------------------------------------

M3C2_PARAMS_TEMPLATE = """[General]
M3C2VER=1
NormalScale={normal_scale}
NormalMode=0
NormalMinScale=0.444358
NormalStep=0.444358
NormalMaxScale=1.777432
NormalUseCorePoints=false
NormalPreferedOri=0
SearchScale={search_scale}
SearchDepth={search_depth}
SubsampleRadius=0.222179
SubsampleEnabled=false
RegistrationError={registration_error}
RegistrationErrorEnabled=true
UseSinglePass4Depth=false
PositiveSearchOnly=false
UseMedian=true
UseMinPoints4Stat=false
MinPoints4Stat=5
ProjDestIndex=1
UseOriginalCloud=false
ExportStdDevInfo=false
ExportDensityAtProjScale=false
MaxThreadCount=14
UsePrecisionMaps=false
PM1Scale=1
PM2Scale=1
"""


def generate_m3c2_params_file(output_path, normal_scale, search_scale,
                               search_depth, registration_error):
    """
    Writes a CloudCompare M3C2 params .txt, based on a confirmed-working
    reference file - only the four values known to matter for this
    workflow are parameterized; everything else is copied verbatim from
    that reference. See generate_m3c2_params.py's module docstring for
    the full rationale.
    """
    content = M3C2_PARAMS_TEMPLATE.format(
        normal_scale=normal_scale,
        search_scale=search_scale,
        search_depth=search_depth,
        registration_error=registration_error,
    )
    Path(output_path).write_text(content, encoding="utf-8")
    return output_path


def read_m3c2_normal_scale(params_file):
    """
    Reads the NormalScale= value out of a CloudCompare M3C2 params .txt
    (the format generate_m3c2_params_file() writes, and CloudCompare's
    own GUI saves). Returns a float, or None if the file is missing,
    unreadable, or has no numeric NormalScale line. Never raises - this
    only feeds a pre-fill (Stage 8's Extract Damage Detail radius), it
    never gates a run.
    """
    if not params_file:
        return None
    try:
        text = Path(params_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"^\s*NormalScale\s*=\s*([-+0-9.eE]+)\s*$", text, re.MULTILINE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def read_m3c2_search_scale(params_file):
    """
    Reads the SearchScale= value (the M3C2 projection scale, the DIAMETER
    of the projection cylinder) out of a CloudCompare M3C2 params .txt. Half
    of it is the projection radius that Stage 6's hole size correction
    needs: M3C2 finds valid values up to that distance inside a hole, so a
    measured hole is smaller than the real one by that radius on each side.
    Returns a float, or None if the file is missing, unreadable, or has no
    numeric SearchScale line. Never raises - same contract as
    read_m3c2_normal_scale(): it only feeds a pre-fill, it never gates a run.
    """
    if not params_file:
        return None
    try:
        text = Path(params_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"^\s*SearchScale\s*=\s*([-+0-9.eE]+)\s*$", text, re.MULTILINE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def run_streaming(cmd, on_line, on_done, cwd=None, on_process=None):
    """
    Run `cmd` in a background thread. Calls on_line(str) for each line of
    combined stdout/stderr as it arrives, and on_done(returncode) once the
    process exits. Safe to call from a GUI main thread - it does not block.

    on_process: optional callable, called ONCE with the subprocess.Popen
    object right after it starts (from the background thread). A caller
    that wants a Stop button keeps this handle and passes it to
    terminate_process_tree(). Optional, so every existing caller (the
    Tkinter applet included) is unaffected.
    """

    def worker():
        try:
            use_shell = sys.platform.startswith("win")
            # On Windows, passing a list with shell=True has unreliable
            # interaction with cmd.exe's own quote-handling - paths with
            # spaces can get split into multiple arguments. Building the
            # fully-quoted command string ourselves via list2cmdline (the
            # same quoting Windows' CreateProcess expects) and passing that
            # string directly is the documented, reliable approach.
            cmd_to_run = subprocess.list2cmdline(cmd) if use_shell else cmd
            # PYTHONUNBUFFERED - without this, a Python-based subprocess's
            # own print() calls sit in ITS internal stdout buffer (since
            # stdout isn't a real terminal once piped like this) until that
            # buffer fills or the process exits - completely independent of
            # bufsize below, which only controls how WE read the pipe, not
            # how the child writes to it. Confirmed cause of a script
            # looking "frozen" while actually running fine (first seen on
            # slam_kiss_icp.py, recurred on segment_planes.py). Harmless
            # no-op for a non-Python command (ouster-cli, CloudCompare)
            # since they simply ignore an env var they don't recognize -
            # so this is set unconditionally rather than only for commands
            # we know are Python.
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            popen_kwargs = {}
            if not use_shell:
                # POSIX only: put the child in its own process group, so
                # terminate_process_tree() can stop it AND anything it
                # started with one os.killpg() call. Windows uses
                # taskkill /T instead (see terminate_process_tree()).
                popen_kwargs["start_new_session"] = True
            process = subprocess.Popen(
                cmd_to_run,
                cwd=cwd,
                shell=use_shell,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                **popen_kwargs,
            )
            if on_process is not None:
                on_process(process)
            for line in process.stdout:
                on_line(line.rstrip("\n"))
            process.wait()
            on_done(process.returncode)
        except FileNotFoundError as e:
            on_line(f"ERROR: {e}")
            on_done(-1)
        except Exception as e:
            on_line(f"ERROR: {type(e).__name__}: {e}")
            on_done(-1)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


def terminate_process_tree(process):
    """
    Stops a process started by run_streaming() AND every process it
    started. Returns True if a stop was sent, False if the process had
    already exited (or process is None).

    Why a plain process.kill() is not enough: on Windows, run_streaming()
    uses shell=True, so `process` is cmd.exe and the real tool
    (python.exe, CloudCompare.exe) is its CHILD. Killing cmd.exe alone
    leaves the real tool running as an orphan. `taskkill /T` stops the
    whole tree. On POSIX, run_streaming() starts the child in its own
    process group, so one os.killpg() does the same job.

    The on_done callback of run_streaming() still fires after this, with
    a non-zero return code - the caller decides how to report a stop.
    """
    if process is None or process.poll() is not None:
        return False
    if sys.platform.startswith("win"):
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        import signal
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            process.kill()
    return True


def annotate_log_file(log_path, stage_title, cmd):
    """
    Rewrites a CloudCompare -LOG_FILE output in place, wrapping the raw
    console dump with clear section headers (stage name, timestamp, the
    exact command run) so RMS / fitness / stats lines are easier to find
    than in an unlabeled flat log. Call this after the process exits.
    Returns True if a log file was found and annotated, False otherwise.
    """
    log_path = Path(log_path)
    if not log_path.exists():
        return False

    raw = log_path.read_text(encoding="utf-8", errors="replace")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    banner = "=" * 64
    rule = "-" * 64

    annotated = (
        f"{banner}\n"
        f" CloudCompare Run Report - {stage_title}\n"
        f" Generated: {timestamp}\n"
        f"{banner}\n\n"
        f"COMMAND EXECUTED:\n{' '.join(str(c) for c in cmd)}\n\n"
        f"{rule}\n"
        f"RAW CLOUDCOMPARE OUTPUT (unmodified, for reference)\n"
        f"{rule}\n"
        f"{raw}\n"
        f"{rule}\n"
        f"END OF LOG\n"
        f"{rule}\n"
    )
    log_path.write_text(annotated, encoding="utf-8")
    return True


def parse_registration_rms(log_file_path):
    """
    Extracts the ICP registration RMS from a CloudCompare -LOG_FILE output,
    matching a line like 'RMS: 0.147367'. Returns a float, or None if the
    file doesn't exist or no RMS line is found (e.g. no alignment ran, or
    CloudCompare's wording differs from what's expected here).

    Deliberately returns None rather than 0.0 on failure - feeding a
    genuine 0 into M3C2's registration-error parameter collapses its Level
    of Detection (LOD) calculation toward zero, which makes M3C2 flag
    almost every point as "significant". That's worse than not running the
    significance test at all, so callers must treat None as "can't run the
    significance test properly", not as "assume perfect registration".
    """
    log_file_path = Path(log_file_path)
    if not log_file_path.exists():
        return None
    try:
        text = log_file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    match = re.search(r"RMS:\s*([\d.]+)", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# RMS sidecar - a small per-output-file record of its ICP registration RMS,
# so Stage 5 (Diff) can look it up by the file path instead of the value
# only living in a report popup that's already closed by the time it's needed.
# In project mode, the same value also gets written into project.json
# itself (cleanup.icp_rms, Section 8) via complete_stage()'s extra_fields -
# the sidecar keeps working the same way either way, since Section 3.2
# keeps the sidecar file itself in use regardless of project mode.
# ---------------------------------------------------------------------------

def _rms_sidecar_path(ply_path):
    ply_path = Path(ply_path)
    return ply_path.with_name(ply_path.stem + "_rms.json")


def save_rms_sidecar(ply_path, rms, log_file=None):
    sidecar = {
        "rms": rms,
        "source_ply": str(ply_path),
        "log_file": str(log_file) if log_file else None,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _rms_sidecar_path(ply_path).write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    return sidecar


def load_rms_sidecar(ply_path):
    """Returns the sidecar dict for ply_path, or None if it doesn't exist
    or fails to parse."""
    path = _rms_sidecar_path(ply_path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# ROS2 bag inspection - reads a bag's metadata.yaml (no rosbags/ROS install
# needed, just PyYAML) to notice, right when a source folder is picked,
# whether it holds raw Ouster packets rather than already-decoded points.
# This is exactly the situation that otherwise only surfaces later as a
# confusing KISS-ICP crash (its rosbag dataloader's own auto-detect finds
# no PointCloud2 topic and raises a bare KeyError instead of a clear
# message) - pipeline_applet.py's Stage 1 dialog calls this the moment a
# ROS2 bag folder is picked as the SLAM source, and offers to run
# decode_raw_packets.py right there if it looks like it's needed.
# ---------------------------------------------------------------------------

def inspect_rosbag_topics(bag_folder):
    """
    Reads a ROS2 bag folder's metadata.yaml and reports what it finds.
    Returns None if `bag_folder` isn't a ROS2 bag folder at all (no
    metadata.yaml present - e.g. a .pcap file, or an unrelated folder).
    Otherwise returns a dict:
        {
            "topics": [{"name": ..., "type": ..., "count": ...}, ...],
            "has_pointcloud2": bool,
            "pointcloud2_topic": str or None,
            "raw_lidar_topic": str or None,
            "raw_lidar_count": int or None,
            "raw_imu_topic": str or None,
            "metadata_topic": str or None,
        }

    "raw_lidar_topic" / "raw_imu_topic" are guessed from topics typed
    'ouster_sensor_msgs/msg/PacketMsg', preferring one with 'lidar' /
    'imu' in its own name; if there's exactly one PacketMsg-typed topic
    and no name match, it's assumed to be the lidar one. "metadata_topic"
    is guessed from a 'std_msgs/msg/String' topic with 'metadata' in its
    name. These are guesses, not confirmed against every possible
    ouster_ros topic-naming convention - a caller with a topic that
    doesn't match should let the user pick manually rather than trusting
    a wrong guess.
    """
    try:
        import yaml
    except ImportError:
        raise RuntimeError(
            "Reading a ROS2 bag's metadata.yaml needs PyYAML. Run: pip install pyyaml")

    metadata_path = Path(bag_folder) / "metadata.yaml"
    if not metadata_path.exists():
        return None

    try:
        data = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None

    bag_info = (data or {}).get("rosbag2_bagfile_information", {}) or {}
    raw_topics = bag_info.get("topics_with_message_count", []) or []

    topics = []
    for entry in raw_topics:
        meta = entry.get("topic_metadata", {}) or {}
        topics.append({
            "name": meta.get("name"),
            "type": meta.get("type"),
            "count": entry.get("message_count"),
        })

    pointcloud2_topic = next((t["name"] for t in topics
                               if t["type"] == "sensor_msgs/msg/PointCloud2"), None)

    packet_topics = [t for t in topics if t["type"] == "ouster_sensor_msgs/msg/PacketMsg"]
    raw_lidar = next((t for t in packet_topics if "lidar" in (t["name"] or "").lower()), None)
    if raw_lidar is None and len(packet_topics) == 1:
        raw_lidar = packet_topics[0]
    raw_imu = next((t for t in packet_topics if "imu" in (t["name"] or "").lower()), None)

    metadata_topic = next((t["name"] for t in topics
                            if t["type"] == "std_msgs/msg/String"
                            and "metadata" in (t["name"] or "").lower()), None)

    return {
        "topics": topics,
        "has_pointcloud2": pointcloud2_topic is not None,
        "pointcloud2_topic": pointcloud2_topic,
        "raw_lidar_topic": raw_lidar["name"] if raw_lidar else None,
        "raw_lidar_count": raw_lidar["count"] if raw_lidar else None,
        "raw_imu_topic": raw_imu["name"] if raw_imu else None,
        "metadata_topic": metadata_topic,
    }


def build_decode_command(decode_script, bag_folder, output_bag, lidar_topic=None,
                          imu_topic=None, metadata_topic=None, points_topic=None):
    """
    Delegates to decode_raw_packets.py, converting a ROS2 bag of raw
    Ouster packets into a new bag carrying decoded sensor_msgs/msg/
    PointCloud2 messages - see that script's own docstring for why this
    exists and what it does not yet do (IMU decoding).
    """
    cmd = [sys.executable, str(decode_script),
           "--input", str(bag_folder),
           "--output", str(output_bag)]
    if lidar_topic:
        cmd += ["--lidar-topic", str(lidar_topic)]
    if imu_topic:
        cmd += ["--imu-topic", str(imu_topic)]
    if metadata_topic:
        cmd += ["--metadata-topic", str(metadata_topic)]
    if points_topic:
        cmd += ["--points-topic", str(points_topic)]
    return cmd


# ---------------------------------------------------------------------------
# Project-mode completion reporting
# ---------------------------------------------------------------------------

def finish_stage(pipeline, stage_name, output_path, success, error_message=None,
                  extra_fields=None, log_path=None):
    """
    Reports a stage's actual completion or failure to the project, once
    the subprocess launched from a build_X_command()'s returned cmd has
    actually finished running. This can't be done inside build_X_command
    itself - that function returns well before the subprocess starts or
    finishes (see its docstring).

    pipeline: a project_manager.PipelineHandle (the SAME one passed to
    the build_X_command() call that started this run), or None.

    Meant to be called from wherever subprocess completion is actually
    detected - today that's pipeline_applet.py's _on_stage_complete().

    Does nothing at all if pipeline is None, so this is always safe to
    call unconditionally regardless of whether the caller is in project
    mode - no need to guard every call site with an `if pipeline:` check.
    """
    if pipeline is None:
        return
    if success:
        project_manager.complete_stage(pipeline, stage_name, output_path,
                                        extra_fields=extra_fields, log_path=log_path)
    else:
        project_manager.fail_stage(pipeline, stage_name, error_message=error_message)


# ---------------------------------------------------------------------------
# Stage 1: SLAM (ouster-cli)
# ---------------------------------------------------------------------------

def build_slam_command(source, voxel_size, output_ply, meta=None, visualize=False,
                        deskew_method="constant_velocity", map_max_range=None,
                        pipeline=None):
    """
    source: a .pcap file, an OSF file, a ROS1 .bag file, or a ROS2 bag
    folder (containing .db3 + metadata.yaml) - ouster-cli's "source"
    argument auto-detects the type from what it's given, so the command
    shape is identical either way.

    meta: required for .pcap sources. Optional for rosbag sources - the
    Ouster SDK's own bag reader can resolve metadata directly from the
    bag if this is omitted; pass a value here only to override that.
    ROS bag reading uses the 'rosbags' Python package (pure Python, no
    ROS install needed) - it should already come in with ouster-sdk, but
    if you hit an import error for it, 'pip install rosbags' directly.

    deskew_method: passed as --deskew-method. Defaults to constant_velocity
    (SLAM's own default) mainly to silence the repeated console note about
    it being picked implicitly - doesn't change behavior from the default,
    just makes the run's settings explicit/reproducible. Pass None to omit
    the flag entirely and let ouster-cli choose silently as before.

    map_max_range: optional, in meters. Removes points farther than this
    from the sensor from the SAVED map only - SLAM itself still sees the
    full range, which helps registration in open areas. Done by chaining
    ouster-cli's own `clip RANGE :<n>mm` command AFTER `slam` and BEFORE
    `save` (range 0 = invalid point, dropped). The clip command and its
    unit syntax were confirmed from `ouster-cli source X clip --help`
    (ouster-sdk 1.0.1). UNVERIFIED against a real capture: that `save`
    drops the clipped points. Check the first real run's point count.

    pipeline: optional project_manager.PipelineHandle for a baseline or
    scan pipeline (project.baseline_handle() / .scan_handle(scan_id)).
    'source' is always used exactly as given (PROJECT_INPUT_PICKER_PLAN.md
    Section 5.5 - the caller's field is the one source of truth for the
    input, in project mode and manual mode alike; pipeline no longer
    causes it to be overridden). Output path resolution is NOT done here
    (a GUI-layer concern): keep passing output_ply explicitly either way,
    whether that's a manually typed path or one the caller already got
    from project_manager.get_output_path(pipeline, "slam", ".ply").

    When pipeline is given, this also calls project_manager.start_stage()
    immediately - before the subprocess call, since the actual subprocess
    runs moments later via run_streaming(), outside this function. This
    function's return value stays exactly `cmd` (a list) either way, so
    every existing manual-mode caller is unaffected. Once the subprocess
    actually finishes, call finish_stage() (above) with the SAME pipeline
    handle to report success/failure - that can't happen here, since this
    function returns long before the subprocess does.
    """
    if pipeline is not None:
        project_manager.start_stage(pipeline, "slam", params={
            "backend": "ouster",
            "voxel_size": voxel_size,
            "deskew_method": deskew_method,
            "meta": str(meta) if meta else None,
            "map_max_range": map_max_range,
        })

    cmd = ["ouster-cli", "source"]
    if meta:
        cmd += ["--meta", str(meta)]
    cmd += [str(source), "slam", "--voxel-size", str(voxel_size)]
    if deskew_method:
        cmd += ["--deskew-method", str(deskew_method)]
    if map_max_range is not None:
        cmd += ["clip", "RANGE", f":{int(round(float(map_max_range) * 1000))}mm"]
    if visualize:
        cmd += ["viz"]
    cmd += ["save", str(output_ply)]
    return cmd


def _read_kiss_icp_config_value(config_path, section, key):
    """
    Reads `<section>.<key>` from a kiss-icp config YAML (see
    kiss_icp_config_indoor.yaml's own structure - top-level `data` and
    `mapping` sections). Shared by the read_kiss_icp_*() functions below.

    Returns a float, or None if `config_path` is falsy, the file doesn't
    exist, isn't valid YAML, or has no numeric value at that key (e.g. a
    config that leaves voxel_size at kiss-icp's own null/auto-derived
    default - a real, valid state, not an error). Never raises - these
    values are read for informational display and for recording what was
    actually used, not to gate anything.
    """
    if not config_path:
        return None
    path = Path(config_path)
    if not path.exists():
        return None
    try:
        import yaml
    except ImportError:
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return None
    section_data = (data or {}).get(section, {}) or {}
    value = section_data.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def read_kiss_icp_voxel_size(config_path):
    """
    Reads `mapping.voxel_size` from a kiss-icp config YAML. Voxel size
    otherwise only lives inside whatever config file is given, which is
    what makes it hard for Stage 2 (Level)'s distance-threshold guidance
    to know what Stage 1 actually used when KISS-ICP was the backend
    (Ouster CLI's own voxel size is a direct, always-visible field).
    See _read_kiss_icp_config_value() for the None/never-raises contract.
    """
    return _read_kiss_icp_config_value(config_path, "mapping", "voxel_size")


def read_kiss_icp_min_range(config_path):
    """
    Reads `data.min_range` from a kiss-icp config YAML - backs the
    "This config's own min_range" GUI label, so you can tell whether a
    run used the config's own value or your own override. See
    _read_kiss_icp_config_value() for the None/never-raises contract.
    """
    return _read_kiss_icp_config_value(config_path, "data", "min_range")


def read_kiss_icp_max_range(config_path):
    """
    Reads `data.max_range` from a kiss-icp config YAML - backs the
    "This config's own max_range" GUI label. IMPORTANT, verified against
    the kiss-icp 1.2.3 source (VoxelHashMap::Update ->
    RemovePointsFarFromLocation): data.max_range does TWO jobs in
    kiss-icp - it crops each frame, AND it deletes local-map voxels
    farther than max_range from the robot's CURRENT position after every
    frame. That local map only holds points near the robot's final
    position. slam_kiss_icp.py no longer saves that local map by default
    (since update 20 it saves a global map of all frames; --local-map gives
    the old output). Lowering data.max_range shortens the range of every
    frame, and so how far the saved map reaches from the path.
    Since 2026-10-15 the Stage 1 "Map max range" field replaces this
    value for the run (build_kiss_icp_slam_command() passes it as
    --map-max-range, and slam_kiss_icp.py uses it as the registration
    range). This function still gives the CONFIG file's own value.
    See _read_kiss_icp_config_value() for the None/never-raises contract.
    """
    return _read_kiss_icp_config_value(config_path, "data", "max_range")


def build_kiss_icp_slam_command(kiss_icp_script, source, output_ply, config=None,
                                 dataloader=None, topic=None, meta=None,
                                 voxel_size=None, min_range=None, map_max_range=None,
                                 map_max_horizontal=None, map_max_vertical=None,
                                 pipeline=None):
    """
    Delegates to slam_kiss_icp.py - a second, genuinely different SLAM
    backend from ouster-cli's own bundled kiss-icp (same underlying
    algorithm, different entry point/config/input handling), useful for
    comparing output against the same source.

    Confirmed working against real ROS2 bag data (a 247-frame Ouster
    PointCloud2 capture): correct per-point deskewing (verified via the
    dataloader's real source - it reads the message's own 't' field, not
    a coarse per-frame approximation) and correct calibration (verified
    the bag's own /ouster/metadata topic contains valid factory beam
    calibration, matching what ouster-cli would use from a metadata.json).

    config: IMPORTANT - without this, kiss-icp's default voxel_size/
    max_range are tuned for vehicle-scale outdoor odometry (e.g.
    max_range=100m), which drastically under-populates a compartment-
    scale map (confirmed: 2,628 points vs. 503,189 with a properly scaled
    config - roughly 190x difference from this one setting). Strongly
    recommended for any indoor/compartment use - see
    kiss_icp_dump_config for a starting point to edit.

    voxel_size: optional override for the config's own `mapping.voxel_size`
    (or kiss-icp's built-in default, if no config is given at all) -
    passed to slam_kiss_icp.py as --voxel-size, which applies it directly
    to the loaded config object rather than needing a hand-edited copy of
    the YAML just to try a different cell size. NOT YET CONFIRMED against
    a real installed kiss-icp version - slam_kiss_icp.py's own docstring
    flags this the same way; if it fails, edit the config YAML's
    mapping.voxel_size by hand instead and leave this blank. Whether or
    not this override is given, the EFFECTIVE voxel size (this override,
    or else whatever read_kiss_icp_voxel_size(config) finds, or else
    None if genuinely unknown) is what gets recorded in project mode -
    see below.

    min_range: optional override for the config's own `data.min_range`
    (or kiss-icp's own default of 0.0/no cropping, if no config is given
    at all) - passed to slam_kiss_icp.py as --min-range, which crops
    points closer than this before SLAM runs. A robot-mounted sensor
    seeing part of its own mount hardware shows up as a dense, unmoving
    near-sensor blob in every frame - the solver can mistake this fixed
    "geometry" for real structure and align to it instead of the room,
    which can look like noise or a wrong-shaped map. This was previously
    only supported by slam_kiss_icp.py's own CLI flag - this parameter
    (and the "min_range"/"min_range_overridden" params recorded below)
    was the missing link connecting that flag through to project mode
    and the GUI (found and fixed while getting
    test_pipeline_core_project_mode.py's assertions to actually pass
    against real code, not just silencing them - the GUI side, a Stage 1
    field for this, still needs its own separate follow-up). Whether or
    not this override is given, the EFFECTIVE min_range (this override,
    or else whatever read_kiss_icp_min_range(config) finds, or else None
    if genuinely unknown) is what gets recorded in project mode - same
    pattern as voxel_size above.

    map_max_range: optional, in meters - passed to slam_kiss_icp.py as
    --map-max-range. CHANGED 2026-10-15: this now REPLACES the config's
    own data.max_range for the run. It sets the registration range and
    it crops every frame, so it also limits the saved map. (Before this
    change it cropped the saved map only, and registration kept the
    config's range.) The horizontal and vertical limits below still crop
    the saved map only. Recorded in project mode as "map_max_range"
    (None = not used). The "max_range" param is the range that SLAM really
    used: map_max_range when given, else the config's own value.
    "max_range_overridden" tells which one it is.

    map_max_horizontal / map_max_vertical: optional, in meters - passed as
    --map-max-horizontal / --map-max-vertical. The same saved-map-only
    crop, split into horizontal and vertical distance from the sensor
    (map frame - see slam_kiss_icp.py's docstring). KISS-ICP only: the
    Ouster CLI backend's `clip` command works on the RANGE field, which
    has no horizontal/vertical split. Can be combined with map_max_range;
    a point must pass every limit given. Recorded as "map_max_horizontal"
    and "map_max_vertical".

    dataloader/topic: only relevant for rosbag sources - topic selects
    which PointCloud2 topic to read if a bag has more than one (kiss-icp
    auto-selects if there's only one, so this is often unnecessary).

    meta: only relevant for the 'ouster' dataloader (.pcap sources) -
    exact argument shape not fully confirmed, see slam_kiss_icp.py's own
    docstring for current status.

    pipeline: optional project_manager.PipelineHandle for a baseline or
    scan pipeline - same behavior as build_slam_command()'s pipeline
    parameter (see its docstring for the full explanation: source is
    always used exactly as given). Stage 1 has two backends sharing one
    pipeline "slam" stage entry - whichever backend actually runs calls
    start_stage("slam", ...) the same way, so project mode works
    identically regardless of which one the user picks. The recorded
    params' "voxel_size" field is deliberately the same key
    build_slam_command() (Ouster CLI) already uses - so Stage 2 (Level)'s
    distance-threshold guidance can read one field, regardless of which
    backend Stage 1 actually used.
    """
    effective_voxel_size = voxel_size if voxel_size is not None else read_kiss_icp_voxel_size(config)
    effective_min_range = min_range if min_range is not None else read_kiss_icp_min_range(config)

    if pipeline is not None:
        project_manager.start_stage(pipeline, "slam", params={
            "backend": "kiss_icp",
            "config": str(config) if config else None,
            "dataloader": dataloader,
            "topic": topic,
            "voxel_size": effective_voxel_size,
            "voxel_size_overridden": voxel_size is not None,
            "min_range": effective_min_range,
            "min_range_overridden": min_range is not None,
            "max_range": (map_max_range if map_max_range is not None
                          else read_kiss_icp_max_range(config)),
            "max_range_overridden": map_max_range is not None,
            "config_max_range": read_kiss_icp_max_range(config),
            "map_max_range": map_max_range,
            "map_max_horizontal": map_max_horizontal,
            "map_max_vertical": map_max_vertical,
        })

    cmd = [sys.executable, str(kiss_icp_script),
           "--input", str(source),
           "--output", str(output_ply)]
    if config:
        cmd += ["--config", str(config)]
    if dataloader:
        cmd += ["--dataloader", str(dataloader)]
    if topic:
        cmd += ["--topic", str(topic)]
    if meta:
        cmd += ["--meta", str(meta)]
    if voxel_size is not None:
        cmd += ["--voxel-size", str(voxel_size)]
    if min_range is not None:
        cmd += ["--min-range", str(min_range)]
    if map_max_range is not None:
        cmd += ["--map-max-range", str(map_max_range)]
    if map_max_horizontal is not None:
        cmd += ["--map-max-horizontal", str(map_max_horizontal)]
    if map_max_vertical is not None:
        cmd += ["--map-max-vertical", str(map_max_vertical)]
    return cmd


# ---------------------------------------------------------------------------
# Stage 2: Level (corrects SLAM's arbitrary tilt so the floor is horizontal)
# ---------------------------------------------------------------------------

def build_level_command(level_script, input_ply, output_ply,
                         distance_threshold=None, max_planes=None, min_inlier_fraction=None,
                         horizontal_threshold=None, pipeline=None):
    """
    Delegates to level_cloud.py. ouster-cli's SLAM has no gravity/leveling
    step, so the whole map inherits whatever tilt the sensor had at the
    very first frame - this finds the floor via RANSAC and rotates the
    cloud so it's actually level, before Stage 3's ICP alignment (which
    works better on a level cloud) or anything downstream sees it.

    distance_threshold: RANSAC plane-fit tolerance. The script's default
    (0.02) can be too tight if Stage 1's voxel size was coarser than that -
    quantized points won't sit close enough to any plane to be found. Try
    a value around the Stage 1 voxel size, or larger, if no planes are found.

    horizontal_threshold: how close to vertical (|normal.z|) a candidate
    plane must be to even be ELIGIBLE as the floor. The script picks the
    LOWEST candidate that clears this bar, not the biggest one - a real
    ceiling can have more points than a real floor (cleaner surface, less
    clutter), so scoring by point count alone can pick the ceiling by
    mistake, confirmed on real data. Loosen this (lower it) only if a real
    floor/ceiling isn't clearing the bar at all; leave it alone otherwise.

    pipeline: optional project_manager.PipelineHandle for a baseline or
    scan pipeline - see build_slam_command()'s docstring for the full
    explanation (input_ply always used as given, start_stage call, and
    why finish_stage() is needed separately for completion). Manual mode
    (pipeline=None, the default) is completely unaffected.
    """
    if pipeline is not None:
        project_manager.start_stage(pipeline, "level", params={
            "distance_threshold": distance_threshold,
            "max_planes": max_planes,
            "min_inlier_fraction": min_inlier_fraction,
            "horizontal_threshold": horizontal_threshold,
        })

    cmd = [sys.executable, str(level_script),
           "--input", str(input_ply),
           "--output", str(output_ply)]
    if distance_threshold is not None:
        cmd += ["--distance-threshold", str(distance_threshold)]
    if max_planes is not None:
        cmd += ["--max-planes", str(max_planes)]
    if min_inlier_fraction is not None:
        cmd += ["--min-inlier-fraction", str(min_inlier_fraction)]
    if horizontal_threshold is not None:
        cmd += ["--horizontal-threshold", str(horizontal_threshold)]
    return cmd


# ---------------------------------------------------------------------------
# Stage 4 (project stage name "segment"): classifies a cleaned cloud's
# points into floor/ceiling/wall_N/unclassified via segment_planes.py -
# added after Cleanup in the baseline/scan pipeline (PROJECT_SCHEMA_v2.md
# Section 8/9/10.2). Runs once per baseline/scan, not per diff - as of
# Update 12, Stage 5 (Diff)'s get_diff_inputs() prefers THIS stage's own
# output over cleanup.output once it has completed for a side, falling
# back to cleanup.output only when it hasn't (Section 11.3).
# ---------------------------------------------------------------------------

def build_segment_command(segment_script, input_ply, output_dir,
                           distance_threshold=0.05, max_planes=20,
                           horizontal_threshold=0.7, max_horizontal_z_span=0.3,
                           min_inlier_fraction=0.003, cluster_filter=True,
                           cluster_eps=0.5, cluster_min_points=20,
                           merge_coplanar=True, merge_normal_cos=0.98,
                           merge_distance=0.1, write_separate_surfaces=False,
                           envelope_filter=True, envelope_margin=0.15,
                           write_envelope_filtered=False,
                           pipeline=None):
    """
    Delegates to segment_planes.py, classifying input_ply's points into
    floor/ceiling/wall_N/unclassified (a height + orientation heuristic -
    see that script's own docstring for the full explanation, including
    its stray-point cluster filter and split-detection merge, both on by
    default here to match the script's own defaults).

    envelope_filter/envelope_margin/write_envelope_filtered: the "is this
    unclassified point real interior content, or junk that sits outside
    the room altogether" split - see segment_planes.py's own docstring
    and its --envelope-margin help text for the full explanation.
    envelope_filter is on by default (matching the script's own default)
    and always flags classified.ply's 'outside_envelope' field either
    way; write_envelope_filtered additionally writes a second,
    already-junk-removed <name>_envelope_filtered.ply, off by default
    since classified.ply already carries every point with the field
    intact (labeled, not removed) - turn this on for a ready-to-use
    filtered cloud without re-filtering classified.ply by hand.

    Unlike every other stage here, segment_planes.py writes into
    output_dir (a folder), not a single output file. What it writes there
    depends on write_separate_surfaces: off by default, in which case it
    writes only the combined classified.ply (every point, with a
    'classification' field), an envelope.ply, and a manifest.json - one
    cloud, not one file per detected surface. Turn write_separate_surfaces
    on to also get each surface's own .ply file (floor.ply, wall_1.ply,
    etc.) and unclassified.ply alongside those, e.g. for visually tuning
    parameters per surface, or so each surface becomes its own separate
    prim in Omniverse. Either way, call resolve_segment_output(output_dir)
    after the subprocess finishes to read back what it produced
    (PROJECT_SCHEMA_v2.md Section 13.3) - each surface's 'file' entry in
    the manifest is null when write_separate_surfaces was off.

    pipeline: optional project_manager.PipelineHandle for a baseline or
    scan pipeline - same behavior as build_cleanup_command()'s pipeline
    parameter (see build_slam_command()'s docstring for the full
    explanation). Manual mode (pipeline=None, the default) is completely
    unaffected. Segment is this pipeline's LAST stage (Section 8.3) - no
    other stage reads segment's own output as its input by default.
    """
    if pipeline is not None:
        project_manager.start_stage(pipeline, "segment", params={
            "distance_threshold": distance_threshold,
            "max_planes": max_planes,
            "horizontal_threshold": horizontal_threshold,
            "max_horizontal_z_span": max_horizontal_z_span,
            "min_inlier_fraction": min_inlier_fraction,
            "cluster_filter": cluster_filter,
            "cluster_eps": cluster_eps if cluster_filter else None,
            "cluster_min_points": cluster_min_points if cluster_filter else None,
            "merge_coplanar": merge_coplanar,
            "merge_normal_cos": merge_normal_cos if merge_coplanar else None,
            "merge_distance": merge_distance if merge_coplanar else None,
            "write_separate_surfaces": write_separate_surfaces,
            "envelope_filter": envelope_filter,
            "envelope_margin": envelope_margin if envelope_filter else None,
            "write_envelope_filtered": write_envelope_filtered,
        })

    cmd = [sys.executable, str(segment_script),
           "--input", str(input_ply),
           "--output-dir", str(output_dir),
           "--distance-threshold", str(distance_threshold),
           "--max-planes", str(max_planes),
           "--horizontal-threshold", str(horizontal_threshold),
           "--max-horizontal-z-span", str(max_horizontal_z_span),
           "--min-inlier-fraction", str(min_inlier_fraction)]
    if cluster_filter:
        cmd += ["--cluster-eps", str(cluster_eps), "--cluster-min-points", str(cluster_min_points)]
    else:
        cmd.append("--no-cluster-filter")
    if merge_coplanar:
        cmd += ["--merge-normal-cos", str(merge_normal_cos), "--merge-distance", str(merge_distance)]
    else:
        cmd.append("--no-merge-coplanar")
    if write_separate_surfaces:
        cmd.append("--write-separate-surfaces")
    if envelope_filter:
        cmd += ["--envelope-margin", str(envelope_margin)]
        if write_envelope_filtered:
            cmd.append("--write-envelope-filtered")
    else:
        cmd.append("--no-envelope-filter")
    return cmd


def resolve_segment_output(output_dir):
    """
    Reads segment_planes.py's manifest.json from output_dir, once a
    build_segment_command() run has finished - this is what turns that
    file's content into the record a caller can pass straight into
    finish_stage()'s extra_fields, absorbing the manifest.json pattern
    into project.json per PROJECT_SCHEMA_v2.md Section 3.3, instead of
    every future reader needing to know a separate file exists at all.

    Returns (classified_path, extra_fields):
      - classified_path: a Path to classified.ply (the file this stage's
        own "output" should be recorded as - Section 13.3), or None if
        manifest.json is missing, unreadable, or has no
        classified_cloud_file entry (the run failed or produced nothing
        usable - the caller should already be treating that as a failure
        via the subprocess's own exit code; this is not a second error
        path, just "nothing more to report").
      - extra_fields: a dict with whichever of "envelope_output"
        (string), "envelope_filtered_output" (string, only present if
        write_envelope_filtered was on and at least one point was
        actually flagged outside the envelope), "envelope_filter_applied"
        (bool, always present - true only when there was enough envelope
        surface to actually derive a room footprint/height range from),
        "envelope_margin_used" (number, only present when
        envelope_filter_applied is true - the margin that was actually
        applied), "n_outside_envelope" (int, only present when
        envelope_filter_applied is true), "classification_ids"
        (dict), "surfaces" (list) manifest.json actually had - empty if
        classified_path is None. Paths inside (envelope_output,
        envelope_filtered_output, and each surface's "file") are plain
        strings exactly as segment_planes.py wrote them (absolute, if
        output_dir was passed as absolute) - the caller converts these
        to project-relative paths itself (project_manager.to_relative_path),
        the same way every other stage's dialog already does for its own
        extra_fields (e.g. Cleanup's sidecar path) rather than this
        project_manager-optional module doing that conversion itself.
    """
    manifest_path = Path(output_dir) / "manifest.json"
    if not manifest_path.exists():
        return None, {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, {}

    classified_path = manifest.get("classified_cloud_file")
    if not classified_path:
        return None, {}

    extra_fields = {}
    envelope_path = manifest.get("envelope_cloud_file")
    if envelope_path:
        extra_fields["envelope_output"] = envelope_path
    envelope_filtered_path = manifest.get("envelope_filtered_cloud_file")
    if envelope_filtered_path:
        extra_fields["envelope_filtered_output"] = envelope_filtered_path
    # Bug fix: this used to read envelope_filter_applied from the manifest
    # only as a condition for n_outside_envelope, without ever actually
    # copying the value itself into extra_fields - so it was never recorded
    # on the stage entry at all, contradicting PROJECT_SCHEMA_v2.md Section 8
    # ("envelope_filter_applied ... recorded as false" in the negative case -
    # always present, not omitted). Always set now, true or false.
    # envelope_margin_used was missing entirely (not even attempted) - also
    # added, reading from the manifest's own "envelope_margin" key (that's
    # segment_planes.py's naming; project.json's schema name is
    # "envelope_margin_used" - this function's whole job is exactly this
    # kind of manifest.json-to-project.json translation, per its own
    # docstring above).
    envelope_filter_applied = bool(manifest.get("envelope_filter_applied"))
    extra_fields["envelope_filter_applied"] = envelope_filter_applied
    if envelope_filter_applied:
        extra_fields["envelope_margin_used"] = manifest.get("envelope_margin")
        extra_fields["n_outside_envelope"] = manifest.get("n_outside_envelope", 0)
    classification_ids = manifest.get("classification_ids")
    if classification_ids:
        extra_fields["classification_ids"] = classification_ids
    surfaces = manifest.get("surfaces")
    if surfaces:
        extra_fields["surfaces"] = surfaces

    return Path(classified_path), extra_fields


# ---------------------------------------------------------------------------
# Stage 3: Cleanup (CloudCompare - SOR outlier removal, optional ICP align)
# ---------------------------------------------------------------------------

ICP_OVERLAP_MIN = 10
ICP_OVERLAP_MAX = 100


def parse_icp_overlap(text):
    """Final overlap for ICP, in percent. Blank gives None (CloudCompare keeps
    its own default). Otherwise a whole number from 10 to 100, else ValueError."""
    if text is None:
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        raw = str(text)
    else:
        raw = str(text).strip()
    if raw == "":
        return None
    try:
        value = float(raw)
    except ValueError:
        raise ValueError("ICP final overlap must be a whole number from "
                         f"{ICP_OVERLAP_MIN} to {ICP_OVERLAP_MAX} (percent), "
                         "or blank.")
    if (value != value or value in (float("inf"), float("-inf")) or value != int(value)
            or not (ICP_OVERLAP_MIN <= value <= ICP_OVERLAP_MAX)):
        raise ValueError("ICP final overlap must be a whole number from "
                         f"{ICP_OVERLAP_MIN} to {ICP_OVERLAP_MAX} (percent), "
                         "or blank.")
    return int(value)


def build_cleanup_command(input_ply, output_ply, sor_neighbors=6, sor_std_dev=1.0,
                           align_to_ply=None, log_file=None, pipeline=None,
                           icp_overlap=None):
    """
    If align_to_ply is given, runs ICP to register input_ply onto it after
    outlier removal (input_ply is treated as the moving cloud). This is
    how a comparison scan's cleanup run picks up an icp_rms value at all
    (PROJECT_SCHEMA_v2.md Section 3.2 - Stage 3 writes the RMS sidecar,
    and in project mode that value also lands in this stage's own
    project.json entry, feeding Stage 5's registration_error_used,
    Section 11.3).

    output_ply is the path you WANT the result at, but CloudCompare doesn't
    actually get told to save there directly - see the note below.

    Uses plain -SAVE_CLOUDS (CloudCompare names the file itself), the same
    proven-reliable approach Stage 5 (Diff) already uses. An earlier
    version tried to force an exact filename via -AUTO_SAVE OFF +
    -SAVE_CLOUDS FILE <name>, but that flag's file-count-vs-cloud-count
    behavior proved unreliable in real testing (mismatched in both
    directions across two separate real runs, with no spaces or other
    obvious cause) - not worth building around further. Instead, the
    caller (see resolve_cleanup_output) detects whatever new .ply
    CloudCompare actually created and renames it to output_ply afterward.

    If align_to_ply is given, CloudCompare saves BOTH loaded clouds (it
    saves everything in the DB tree together) - the baseline gets resaved
    alongside the real result, same underlying CloudCompare CLI behavior
    as Stage 5's M3C2 duplicate-file output.

    If log_file is given, passes -LOG_FILE so CloudCompare writes its full
    console output (including ICP RMS / fitness stats) to that path - use
    annotate_log_file() afterwards to wrap it with readable headers.

    pipeline: optional project_manager.PipelineHandle for a baseline or
    scan pipeline. input_ply is always used exactly as given (see
    build_slam_command()'s docstring for the full explanation) -
    align_to_ply likewise is never auto-resolved; whether a comparison
    scan's cleanup should align to the project's baseline, or to some
    other reference cloud, is a per-run decision the caller/dialog makes
    (e.g. by passing project_manager.get_baseline_cleanup_output(project)
    explicitly). start_stage() is still called immediately when pipeline
    is given, recording align_to_ply (if any) in this run's params
    either way.

    icp_overlap: optional whole number from 10 to 100 (percent). It is passed
    to CloudCompare as -OVERLAP, after -ICP. ICP then uses only that share of
    the points (the best-matching ones) for the final fit, so the worst-matching
    points (for example junk points) are left out. None = no -OVERLAP flag,
    CloudCompare keeps its own default. It has no effect (and it is not
    recorded) when align_to_ply is not given, because no ICP runs then.
    """
    icp_overlap = parse_icp_overlap(icp_overlap)
    if pipeline is not None:
        project_manager.start_stage(pipeline, "cleanup", params={
            "sor_neighbors": sor_neighbors,
            "sor_std_dev": sor_std_dev,
            "align_to": str(align_to_ply) if align_to_ply else None,
            "icp_overlap": icp_overlap if align_to_ply else None,
        })

    cmd = ["CloudCompare", "-SILENT"]
    if log_file:
        cmd += ["-LOG_FILE", str(log_file)]

    if align_to_ply:
        cmd += ["-O", str(align_to_ply), "-O", str(input_ply)]
    else:
        cmd += ["-O", str(input_ply)]

    cmd += ["-SOR", str(sor_neighbors), str(sor_std_dev)]
    if align_to_ply:
        cmd += ["-ICP", "-REFERENCE_IS_FIRST"]
        if icp_overlap is not None:
            cmd += ["-OVERLAP", str(icp_overlap)]
    cmd += ["-C_EXPORT_FMT", "PLY", "-SAVE_CLOUDS"]
    return cmd


def find_new_ply_files(directory, existing_before):
    """Returns .ply files in `directory` that weren't present in the
    `existing_before` snapshot, sorted oldest-to-newest by modification
    time - used to spot CloudCompare's auto-named output(s) after a run."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    now_files = set(directory.glob("*.ply"))
    new_files = list(now_files - set(existing_before))
    return sorted(new_files, key=lambda p: p.stat().st_mtime)


def resolve_cleanup_output(input_ply, directory, existing_before, desired_output_path):
    """
    Finds whatever new .ply file(s) CloudCompare actually created during a
    Stage 3 run and renames the one that's almost certainly the real
    cleaned/aligned result (its filename starts with the input cloud's own
    stem, since CloudCompare extends rather than replaces the loaded
    cloud's name) to desired_output_path - so the rest of the pipeline
    still gets the exact filename you asked for, without depending on
    CloudCompare's unreliable explicit-naming flag.

    Returns (resolved_path_or_None, other_new_files, error_message_or_None).
    other_new_files are new .ply files that weren't chosen (e.g. a
    resaved baseline copy when align_to was used) - left as CloudCompare
    named them, not renamed or deleted.
    """
    new_files = find_new_ply_files(directory, existing_before)
    if not new_files:
        return None, [], "No new .ply files appeared after the run - it may have failed silently."

    input_stem = Path(input_ply).stem
    candidates = [f for f in new_files if f.stem.startswith(input_stem)]

    if candidates:
        chosen = candidates[-1]  # most recently modified match
    elif len(new_files) == 1:
        chosen = new_files[0]
    else:
        names = ", ".join(f.name for f in new_files)
        return None, new_files, (
            f"Found {len(new_files)} new .ply file(s) but couldn't confidently tell "
            f"which is the cleaned/aligned result: {names}. Check them manually."
        )

    desired_output_path = Path(desired_output_path)
    desired_output_path.parent.mkdir(parents=True, exist_ok=True)
    if chosen.resolve() != desired_output_path.resolve():
        if desired_output_path.exists():
            desired_output_path.unlink()
        chosen.rename(desired_output_path)
        chosen = desired_output_path

    others = [f for f in new_files if f.exists() and f.resolve() != chosen.resolve()]
    return chosen, others, None


# ---------------------------------------------------------------------------
# Stage 5: Diff (CloudCompare M3C2)
# ---------------------------------------------------------------------------

def build_diff_command(baseline_ply, comparison_ply, m3c2_params_file, log_file=None,
                        pipeline=None):
    """
    m3c2_params_file: path to a CloudCompare M3C2 params .txt. Can come
    from either CloudCompare's own GUI (Plugins > M3C2 Distance, then its
    save/export icon) or generate_m3c2_params_file() below, which writes
    one directly from four key values (normal scale, search scale, search
    depth, registration error) using a confirmed-working reference file
    as the template for everything else. The applet's Stage 5 dialog has
    a "Generate Params File..." button that calls the latter directly.

    If log_file is given, passes -LOG_FILE so CloudCompare's console
    output (core point counts, timing, etc.) is written to that path.

    pipeline: optional project_manager.PipelineHandle for a DIFF pipeline
    (project.diff_handle(diff_id)). baseline_ply and comparison_ply are
    always used exactly as given (PROJECT_INPUT_PICKER_PLAN.md Section
    5.5) - the caller resolves which cleanup/segment output each side
    should actually use, e.g. via project_manager.list_side_candidates(),
    since a diff pipeline's first stage genuinely has two independent
    inputs (either side can be the project's baseline or any earlier
    comparison scan), NOT a single "previous stage" the way every other
    stage here works.

    m3c2_params_file is NOT auto-resolved even in project mode - build one
    with generate_m3c2_params_file() (feeding it
    get_diff_inputs(pipeline)["registration_error_used"] as the
    registration-error value) or point at one made via CloudCompare's own
    GUI, same as manual mode. Call project_manager.get_diff_inputs(pipeline)
    yourself beforehand if the resolved paths/registration error are
    needed for a report or to build that params file - it's a pure read,
    safe to call as many times as needed.

    Cloud load order matters here and is NOT arbitrary. CloudCompare's
    -M3C2 CLI command treats the FIRST cloud passed via -O as "Cloud #1",
    the compared cloud - this is the cloud that receives the M3C2 distance
    scalar field (its points become the "core points" M3C2 actually
    measures at), and the one -SAVE_CLOUDS writes back out (with an
    "_M3C2_<timestamp>" suffix, into that cloud's OWN input folder, not
    wherever the pipeline's desired output lives). The second -O cloud,
    "Cloud #2", is only used as the static reference surface and is not
    normally resaved with a result attached.

    baseline_ply is loaded FIRST (compared/core points) and
    comparison_ply SECOND (reference), a deliberate choice: it keeps the
    M3C2 result anchored to the same fixed set of query locations (the
    baseline's own points) across every diff run against that baseline,
    which is what reliably catches material LOSS - a hole or missing
    chunk shows up as a large gap measured from a baseline point that
    still exists, even where the comparison scan has few or no points of
    its own left in that spot. The trade-off, kept deliberately for now:
    geometry that only EXISTS in the newer comparison scan (added debris,
    an outward bulge) has no baseline point to anchor a core point on, so
    it can be under-reported. Running M3C2 a second time with the load
    order reversed (comparison as core points) and merging both result
    sets would close that gap - not implemented yet; see
    PROJECT_INTEGRATION_PLAN.md Section 6 for what that would involve.

    Because baseline_ply is Cloud #1 here, the M3C2 result attaches to
    and resaves via baseline_ply's own file, in baseline_ply's own
    folder - confirmed directly from a CloudCompare run log showing the
    "_M3C2_" result file appearing under baseline/04_cleanup/. The
    applet's Stage 5 dialog watches THAT folder
    (Path(baseline).resolve().parent) for the new file after the
    CloudCompare subprocess exits, matching this order - an earlier
    version of the dialog watched comparison_ply's folder instead, which
    is why the result appeared to save "in the wrong place": the two
    sides disagreed about which cloud CloudCompare would actually treat
    as Cloud #1.
    """
    if pipeline is not None:
        # normal_scale is recorded here (read back out of the params
        # file itself) so Stage 8's Extract Damage Detail radius can
        # pre-fill from stages.diff.params.normal_scale - the key path
        # both the Tkinter and Qt apps read. Before this, nothing
        # recorded it, so that pre-fill was always blank in both apps.
        # reference_input / comparison_input (update 6): the two clouds
        # this run actually used. Stage 8 reads reference_input to label
        # damage sites with their Stage 4 surface (usd_export.py
        # --surfaces). Project-relative when inside the project.
        def _project_path(path):
            try:
                return project_manager.to_relative_path(pipeline.project, path)
            except project_manager.ProjectError:
                return str(path)

        project_manager.start_stage(pipeline, "diff", params={
            "m3c2_params_file": str(m3c2_params_file),
            "normal_scale": read_m3c2_normal_scale(m3c2_params_file),
            "reference_input": _project_path(baseline_ply),
            "comparison_input": _project_path(comparison_ply),
        })

    cmd = ["CloudCompare", "-SILENT"]
    if log_file:
        cmd += ["-LOG_FILE", str(log_file)]
    cmd += ["-O", str(baseline_ply), "-O", str(comparison_ply),
            "-M3C2", str(m3c2_params_file),
            "-C_EXPORT_FMT", "PLY", "-SAVE_CLOUDS"]
    return cmd


# ---------------------------------------------------------------------------
# Stage 6: Classify (thresholds the M3C2 result into real change vs noise)
# ---------------------------------------------------------------------------

def build_classify_command(classify_script, input_ply, output_ply, threshold=None, keep_all=False,
                            cluster=True, cluster_method="dbscan", cluster_eps=0.05,
                            cluster_min_samples=4, min_cluster_size=4, use_uncertainty=False,
                            pipeline=None, find_holes=False, hole_min_points=100,
                            hole_min_width=0.05, projection_radius=0.0,
                            interior_point=None, hole_value=0.10):
    """
    Delegates to m3c2_classify.py, which drops (or flags) points below the
    given distance threshold so the change-highlight cloud fed into
    Stage 7 only shows real change, not the whole M3C2 result.

    Since m3c2_classify.py also added spatial clustering (Step B/C: DBSCAN
    or HDBSCAN on just the flagged points, isolated "noise" points treated
    as a second false-positive filter) and per-cluster aggregation
    (Step D: centroid/count/extent/magnitude per damage site), cluster is
    on by default here too, matching that script's own default. Turning
    it off (cluster=False) restores the original threshold-only behavior.

    threshold: required UNLESS use_uncertainty=True, matching
    m3c2_classify.py's own argparse validation - raises ValueError here
    too (rather than deferring to the subprocess) so a caller finds out
    immediately, before spawning anything.

    use_uncertainty: use m3c2_classify.py's --use-uncertainty mode
    instead - CloudCompare's own per-point M3C2 uncertainty field (its
    LOD95, 1.96 x (local roughness + registration error)) rather than
    one fixed distance everywhere. If threshold is ALSO given, a point
    must clear both (an extra floor on top of the per-point test) - see
    m3c2_classify.py's own --use-uncertainty --help text for why this
    exists: a single fixed threshold has to clear the worst local
    uncertainty anywhere in the cloud, which can bury real change in the
    scan's better-registered, lower-noise regions.

    m3c2_classify.py writes its Step D per-cluster summary as a sidecar
    file next to output_ply (same name, '.clusters.json' extension) when
    clustering is enabled - see resolve_classify_output(), the companion
    function that reads it back after the subprocess finishes, mirroring
    resolve_segment_output()'s manifest-absorption pattern (Section 3.3).

    find_holes: also find holes (points where the M3C2 distance has no value,
    because the new scan has no points there) and the direction of change
    (added or removed) - m3c2_classify.py's --find-holes. Needs clustering.
    Adds the output fields change_inward and change_type, and a "type"
    ("added", "removed" or "hole") to every cluster in the .clusters.json.
    The rest of the hole values (all in METRES): hole_min_points (a count),
    hole_min_width (the shortest side a hole may have), projection_radius
    (half of the M3C2 Search scale, for the corrected hole size; 0 = no
    correction), interior_point ((x, y, z) of a point inside the room, or
    None for the centre of all points) and hole_value (the value that
    change_inward shows for a hole - display only).

    pipeline: optional project_manager.PipelineHandle for a DIFF pipeline.
    input_ply is always used exactly as given (see build_slam_command()'s
    docstring for the full explanation) - only start_stage() tracking
    happens here now. (It is the last positional parameter; the find_holes
    values follow it so that no existing call changes.)
    """
    if threshold is None and not use_uncertainty:
        raise ValueError("threshold is required unless use_uncertainty=True.")
    if find_holes and not cluster:
        raise ValueError("Find holes needs clustering. Turn clustering on, or turn "
                         "Find holes off.")

    if pipeline is not None:
        project_manager.start_stage(pipeline, "classify", params={
            "threshold": threshold,
            "use_uncertainty": use_uncertainty,
            "keep_all": keep_all,
            "cluster": cluster,
            "cluster_method": cluster_method if cluster else None,
            "cluster_eps": cluster_eps if (cluster and cluster_method == "dbscan") else None,
            "cluster_min_samples": cluster_min_samples if cluster else None,
            "min_cluster_size": min_cluster_size if cluster else None,
            "find_holes": bool(find_holes),
            "hole_min_points": hole_min_points if find_holes else None,
            "hole_min_width": hole_min_width if find_holes else None,
            "projection_radius": projection_radius if find_holes else None,
            "interior_point": list(interior_point) if (find_holes and interior_point) else None,
            "hole_value": hole_value if find_holes else None,
        })

    cmd = [sys.executable, str(classify_script),
           "--input", str(input_ply),
           "--output", str(output_ply)]
    if threshold is not None:
        cmd += ["--threshold", str(threshold)]
    if use_uncertainty:
        cmd.append("--use-uncertainty")
    if keep_all:
        cmd.append("--keep-all")
    if cluster:
        cmd += ["--cluster-method", str(cluster_method),
                "--cluster-min-samples", str(cluster_min_samples),
                "--min-cluster-size", str(min_cluster_size)]
        if cluster_method == "dbscan":
            cmd += ["--cluster-eps", str(cluster_eps)]
    else:
        cmd.append("--no-cluster")
    if find_holes:
        cmd += ["--find-holes",
                "--min-hole-points", str(int(hole_min_points)),
                "--min-hole-width", str(hole_min_width),
                "--hole-value", str(hole_value)]
        if projection_radius:
            cmd += ["--projection-radius", str(projection_radius)]
        if interior_point:
            cmd += ["--interior-point"] + [str(float(v)) for v in interior_point]
    return cmd


def classify_clusters_path(output_ply):
    """Where m3c2_classify.py writes its per-cluster summary for a given
    Classify output: the same name with a '.clusters.json' extension.
    resolve_classify_output() reads this file; Stage 8 passes it to
    usd_export.py --clusters."""
    return Path(output_ply).with_suffix(".clusters.json")


def resolve_classify_output(output_ply):
    """
    Reads the '*.clusters.json' sidecar m3c2_classify.py writes next to
    its output when clustering is enabled (Section 3.3's manifest-
    absorption pattern - the same idea as resolve_segment_output(), but
    classify's own output path is already known up front, so this only
    needs to return extra_fields, not resolve the output path itself).

    Returns {} (never raises) if no sidecar exists - clustering was
    disabled with cluster=False/--no-cluster, or the run failed before
    reaching Step D.

    With Find holes, each entry of "clusters" also has a "type" ("added",
    "removed", "changed", "hole" or "unmatched"), and a hole or unmatched
    entry has its size (width, height, thickness, area) instead of the M3C2
    magnitudes.
    """
    summary_path = classify_clusters_path(output_ply)
    if not summary_path.exists():
        return {}
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    extra_fields = {}
    for key in ("n_flagged", "n_confirmed", "n_noise", "use_uncertainty", "uncertainty_field",
                # Only present when the run used Find holes (m3c2_classify.py
                # --find-holes). n_confirmed / n_noise / n_flagged keep their
                # meaning (measured change only); the points of holes are
                # counted in n_hole_points.
                "find_holes", "direction_found", "n_added_points", "n_removed_points",
                "n_holes", "n_hole_points", "n_missing", "projection_radius",
                # Sites of the type "unmatched": patches with no partner in the
                # other scan (a moved or absent object).
                "find_unmatched", "n_unmatched", "n_unmatched_points",
                # How the inside of the room was found (m3c2_classify.py
                # --find-holes): "probe", "centroid" or "interior-point".
                "interior_method", "n_orient_fallback",
                "orient_disagree_with_centroid"):
        if key in summary:
            extra_fields[key] = summary[key]
    clusters = summary.get("clusters")
    if clusters:
        extra_fields["clusters"] = clusters
    return extra_fields


def read_ply_vertex_properties(ply_path):
    """Names of the properties of the 'vertex' element of a PLY file, read
    from the header only (the file is not loaded). Returns a list, or None if
    the file cannot be read or is not a PLY file. Never raises."""
    try:
        with open(ply_path, "rb") as handle:
            head = handle.read(65536)
    except (OSError, TypeError, ValueError):
        return None
    if not head.startswith(b"ply"):
        return None
    end = head.find(b"end_header")
    if end < 0:
        return None
    names, in_vertex = [], False
    for line in head[:end].decode("ascii", errors="replace").splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "element":
            in_vertex = len(parts) > 1 and parts[1] == "vertex"
        elif parts[0] == "property" and in_vertex and len(parts) >= 3:
            names.append(parts[-1])
    return names


# ---------------------------------------------------------------------------
# Stage 7: Surface (Open3D - reconstructs a mesh from a point cloud). The
# Blender method has its own builder below: build_blender_surface_command().
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Registration estimate from the M3C2 distances (Stage 5)
# ---------------------------------------------------------------------------

M3C2_DISTANCE_FIELD = "scalar_M3C2_distance"


def robust_m3c2_spread(ply_path, field=M3C2_DISTANCE_FIELD):
    """Estimate the registration error from a Stage 5 (M3C2) result.

    The ICP RMS of Stage 3 is the nearest-point distance (point to point) over
    sampled points. M3C2 measures the distance along the surface normal. The
    two numbers can be very different. In a real test the ICP RMS was 5.35 cm
    and the spread of the M3C2 distances was 0.31 cm. The spread of the M3C2
    distances on the surfaces that did not change is the number that the M3C2
    significance test needs. The median and the median absolute deviation
    (MAD) ignore a small share of damaged points.

    Returns None when the file cannot be read, has no `field`, or has fewer
    than 100 valid distances. Otherwise a dict (all distances in metres):
      n_total, n_valid, n_nan   point counts (NaN = no partner, a hole)
      median                    systematic offset between the two scans
      robust_sigma              1.4826 x MAD about the median: the estimate
      median_abs                median of |distance|
      p95_abs                   95th percentile of |distance|
      tail_threshold, tail_share  share of the valid distances that are more
                                than max(5 x robust_sigma, 1 cm) from the median
    Never raises. Needs numpy and plyfile (None when they are not installed).
    The value also holds some surface roughness, so it is an upper estimate
    of the pure registration error.
    """
    try:
        import numpy as np
        from plyfile import PlyData
    except ImportError:
        return None
    try:
        vertex = PlyData.read(str(ply_path))["vertex"]
        if field not in vertex.data.dtype.names:
            return None
        d = np.asarray(vertex[field], dtype=np.float64)
    except Exception:
        return None
    return _spread_statistics(d)


def _spread_statistics(d):
    """Statistics of an array of M3C2-style distances (NaN = no distance).
    Same numbers for the Stage 5 report and for the quick check.
    Returns None when there are fewer than 100 valid values."""
    import numpy as np
    valid = d[np.isfinite(d)]
    if valid.size < 100:
        return None
    med = float(np.median(valid))
    sigma = float(1.4826 * np.median(np.abs(valid - med)))
    tail_threshold = float(max(5.0 * sigma, 0.01))
    return {
        "n_total": int(d.size),
        "n_valid": int(valid.size),
        "n_nan": int(d.size - valid.size),
        "median": med,
        "robust_sigma": sigma,
        "median_abs": float(np.median(np.abs(valid))),
        "p95_abs": float(np.percentile(np.abs(valid), 95)),
        "p99_abs": float(np.percentile(np.abs(valid), 99)),
        "tail_threshold": tail_threshold,
        "tail_share": float(np.mean(np.abs(valid - med) > tail_threshold)),
    }


# Stage 6 threshold check. A fixed threshold of 2 to 3 times the spread assumes
# that the distances follow a bell curve. A real scan can have a heavy tail: the
# middle is quiet but many points are far from zero. Then the same threshold
# flags many more points than the bell curve says.
THRESHOLD_HEAVY_MIN_SHARE = 0.05      # more than 5 % flagged ...
THRESHOLD_HEAVY_RATIO = 5.0           # ... and more than 5 times the bell-curve share


def m3c2_threshold_check(ply_path, threshold, field=M3C2_DISTANCE_FIELD):
    """Compare a Stage 6 distance threshold (metres) with the distances of a
    Stage 5 (M3C2) result file.

    Returns None when the file cannot be read, has no `field`, or has fewer than
    100 valid distances. Otherwise a dict (distances in metres):
      stats                the dict of robust_m3c2_spread()
      threshold            the threshold that was checked
      share_flagged        share of the valid distances with |distance| >= threshold
      share_bell_curve     the share that a bell curve with the same robust spread
                           would give (two-sided, centred on zero)
      heavy_tail           True when share_flagged is more than 5 % and more than
                           5 times share_bell_curve
      percentile_threshold the 95th percentile of |distance|. A threshold with this
                           value flags about 5 % of the points.
    Never raises. Needs numpy and plyfile (None when they are not installed)."""
    try:
        import math
        import numpy as np
        from plyfile import PlyData
    except ImportError:
        return None
    try:
        vertex = PlyData.read(str(ply_path))["vertex"]
        if field not in vertex.data.dtype.names:
            return None
        d = np.asarray(vertex[field], dtype=np.float64)
    except Exception:
        return None
    stats = _spread_statistics(d)
    if stats is None:
        return None
    valid = np.abs(d[np.isfinite(d)])
    share = float(np.mean(valid >= threshold))
    sigma = stats["robust_sigma"]
    bell = math.erfc(threshold / (sigma * math.sqrt(2.0))) if sigma > 0 else 0.0
    heavy = share > THRESHOLD_HEAVY_MIN_SHARE and share > THRESHOLD_HEAVY_RATIO * bell
    return {
        "stats": stats,
        "threshold": float(threshold),
        "share_flagged": share,
        "share_bell_curve": float(bell),
        "heavy_tail": bool(heavy),
        "percentile_threshold": stats["p95_abs"],
    }


def threshold_check_text(check):
    """Text for the Stage 6 threshold check (a multi-line string, in cm)."""
    s = check["stats"]
    lines = [
        f"Threshold: {check['threshold'] * 100:.2f} cm. Spread of the distances "
        f"(robust): {s['robust_sigma'] * 100:.2f} cm.",
        f"This threshold flags {check['share_flagged'] * 100:.1f} % of the points. "
        f"A bell curve with this spread flags {check['share_bell_curve'] * 100:.2f} %.",
        f"Distances: 95 % are smaller than {s['p95_abs'] * 100:.2f} cm and 99 % are "
        f"smaller than {s['p99_abs'] * 100:.2f} cm.",
    ]
    if check["heavy_tail"]:
        lines.append(
            "WARNING: the distances have a heavy tail. Many points are far from zero, "
            "so a threshold of 2 to 3 times the spread flags too many points. These "
            "points can join into very large damage sites. They can be noise, drift or "
            "real change. A threshold from the 95th percentile "
            f"({check['percentile_threshold'] * 100:.2f} cm) flags about 5 % of the "
            "points. Use it as a start and look at the result in CloudCompare.")
    else:
        lines.append("The tail is not heavy. The threshold is a reasonable start.")
    return "\n".join(lines)


QUICK_REGISTRATION_CORE_POINTS = 10000


def quick_registration_estimate(baseline_ply, comparison_ply, normal_scale,
                                search_scale, search_depth,
                                max_core_points=QUICK_REGISTRATION_CORE_POINTS,
                                seed=0):
    """Estimate the registration error BEFORE Stage 5 runs, without CloudCompare.

    It uses the same idea as M3C2 on a random sample of the baseline points
    (the core points):
      1. The normal at a core point: the plane through the baseline points
         inside a sphere of diameter `normal_scale`. The normal points to the
         centre of the baseline.
      2. A cylinder around the normal: diameter `search_scale`, total length
         `search_depth`. The distance is the mean position of the comparison
         points in the cylinder minus the mean position of the baseline
         points in the cylinder, both along the normal. A core point with no
         points of one cloud in the cylinder has no distance (NaN).
      3. The statistics are the same as for a Stage 5 result
         (_spread_statistics), so the numbers can be compared.

    This is an ESTIMATE. It does not use the exact CloudCompare code (for
    example, the length of the cylinder and the handling of the normals can
    differ). Use it to choose the Registration RMS before the run, and use
    the REGISTRATION CHECK of the Stage 5 report to confirm the value.

    All lengths are in metres. Returns (estimate, message). On success
    `estimate` is the dict of robust_m3c2_spread() with the extra keys
    "n_core" and "method" ("quick") and `message` is "". When the estimate
    is not possible, `estimate` is None and `message` says why. Never raises.
    """
    try:
        import numpy as np
        from plyfile import PlyData
        from scipy.spatial import cKDTree
    except ImportError as e:
        return None, (f"The Python package '{e.name}' is not installed. Install numpy, "
                      "scipy and plyfile (see requirements.txt).")
    try:
        normal_scale = float(normal_scale)
        search_scale = float(search_scale)
        search_depth = float(search_depth)
    except (TypeError, ValueError):
        return None, "The normal scale, search scale and search depth must be numbers."
    if min(normal_scale, search_scale, search_depth) <= 0:
        return None, "The normal scale, search scale and search depth must be above 0."

    def read_xyz(path):
        vertex = PlyData.read(str(path))["vertex"]
        return np.column_stack([np.asarray(vertex[axis], dtype=np.float64)
                                for axis in ("x", "y", "z")])

    try:
        base = read_xyz(baseline_ply)
    except Exception as e:
        return None, f"The baseline file cannot be read: {e}"
    try:
        comp = read_xyz(comparison_ply)
    except Exception as e:
        return None, f"The comparison file cannot be read: {e}"
    if len(base) < 100 or len(comp) < 100:
        return None, "A file has fewer than 100 points."

    rng = np.random.default_rng(seed)
    n_core = int(min(max_core_points, len(base)))
    core = base[rng.choice(len(base), n_core, replace=False)]
    tree_base, tree_comp = cKDTree(base), cKDTree(comp)
    centre = base.mean(axis=0)

    radius_cyl = search_scale / 2.0
    half_len = search_depth / 2.0
    radius_ball = float(np.hypot(radius_cyl, half_len))
    normal_neighbours = tree_base.query_ball_point(core, normal_scale / 2.0)
    base_neighbours = tree_base.query_ball_point(core, radius_ball)
    comp_neighbours = tree_comp.query_ball_point(core, radius_ball)

    def along_normal(points, origin, normal):
        """Positions along the normal of the points that are inside the cylinder."""
        v = points - origin
        t = v @ normal
        inside = (np.abs(t) <= half_len) & ((v * v).sum(axis=1) - t * t <= radius_cyl ** 2)
        return t[inside]

    distances = np.full(n_core, np.nan)
    for i in range(n_core):
        idx = normal_neighbours[i]
        if len(idx) < 4:
            continue
        pts = base[idx]
        local = pts - pts.mean(axis=0)
        try:
            _, vectors = np.linalg.eigh(local.T @ local)
        except np.linalg.LinAlgError:
            continue
        normal = vectors[:, 0]
        if normal @ (centre - core[i]) < 0:
            normal = -normal
        t_base = along_normal(base[base_neighbours[i]], core[i], normal) \
            if base_neighbours[i] else ()
        t_comp = along_normal(comp[comp_neighbours[i]], core[i], normal) \
            if comp_neighbours[i] else ()
        if len(t_base) == 0 or len(t_comp) == 0:
            continue
        distances[i] = float(np.mean(t_comp) - np.mean(t_base))

    estimate = _spread_statistics(distances)
    if estimate is None:
        return None, ("Fewer than 100 core points have a distance. The scans may not "
                      "overlap, or the scales are too small for the point spacing. Check "
                      "the Normal scale (use 'Check Point Spacing') and the alignment.")
    estimate["n_core"] = n_core
    estimate["method"] = "quick"
    return estimate, ""


def find_m3c2_result(candidates, field=M3C2_DISTANCE_FIELD):
    """First path in `candidates` whose PLY header has `field`, else None.
    Reads only the headers. Never raises."""
    for path in candidates or []:
        props = read_ply_vertex_properties(path)
        if props and field in props:
            return Path(path)
    return None


def registration_status(entered_rms, estimate):
    """Compare the entered Registration RMS (metres, or None) with the estimate
    of robust_m3c2_spread() or quick_registration_estimate().
    Returns "none" (nothing entered), "too_high" (the RMS is more than 1.5 times
    the spread), "too_low" (less than 0.67 times the spread) or "ok"."""
    if entered_rms is None:
        return "none"
    sigma = estimate["robust_sigma"]
    ratio = entered_rms / sigma if sigma > 0 else float("inf")
    if ratio > 1.5:
        return "too_high"
    if ratio < 0.67:
        return "too_low"
    return "ok"


def registration_headline(entered_rms, estimate):
    """One short line (capital letters) for the top of the Stage 5 report."""
    sigma_cm = estimate["robust_sigma"] * 100
    status = registration_status(entered_rms, estimate)
    if status == "none":
        return f"ENTER {sigma_cm:.2f} cm AS THE REGISTRATION RMS"
    if status == "too_high":
        return (f"CHANGE THE REGISTRATION RMS FROM {entered_rms * 100:.2f} cm "
                f"TO {sigma_cm:.2f} cm AND RUN AGAIN")
    if status == "too_low":
        return (f"CHECK THE ALIGNMENT: THE SPREAD ({sigma_cm:.2f} cm) IS LARGER THAN "
                f"THE REGISTRATION RMS ({entered_rms * 100:.2f} cm)")
    return f"OK: THE REGISTRATION RMS AGREES WITH THE SPREAD ({sigma_cm:.2f} cm)"


def registration_advice(entered_rms, estimate):
    """Text for the Stage 5 report: compare the entered registration RMS (in
    metres, or None) with the estimate from robust_m3c2_spread().
    Returns a short multi-line string."""
    sigma = estimate["robust_sigma"]
    lines = [
        f"Distances on {estimate['n_valid']} of {estimate['n_total']} points "
        f"({estimate['n_nan']} points have no distance).",
        f"Offset between the scans (median distance): {estimate['median'] * 100:+.2f} cm.",
        f"Spread of the distances (robust, 1.4826 x MAD): {sigma * 100:.2f} cm.",
        f"95 % of the distances are smaller than {estimate['p95_abs'] * 100:.2f} cm "
        "(plus or minus).",
    ]
    if entered_rms is None:
        lines.append(
            f"No Registration RMS was entered. You can enter {sigma * 100:.2f} cm "
            "as the Registration RMS.")
    else:
        status = registration_status(entered_rms, estimate)
        if status == "too_high":
            lines.append(
                f"The Registration RMS ({entered_rms * 100:.2f} cm) is much larger than "
                "this spread. Most of the surface agrees better than the RMS says. "
                "ICP measures the nearest-point distance. M3C2 measures the distance "
                "along the surface normal. The two values can be very different. "
                f"Try {sigma * 100:.2f} cm as the Registration RMS, push 'Generate "
                "Params File...' and run Stage 5 again.")
        elif status == "too_low":
            lines.append(
                f"The distances spread more than the Registration RMS "
                f"({entered_rms * 100:.2f} cm) says. The alignment can be poor, or the "
                "surfaces are rough. Check the alignment in CloudCompare before you "
                f"trust small changes. Compare with {sigma * 100:.2f} cm.")
        else:
            lines.append(
                f"The Registration RMS ({entered_rms * 100:.2f} cm) agrees with the "
                "spread of the distances.")
    if estimate.get("tail_share", 0.0) > 0.02:
        lines.append(
            f"{estimate['tail_share'] * 100:.1f} % of the distances are more than "
            f"{estimate['tail_threshold'] * 100:.1f} cm from the median. The spread "
            "above does not describe these points. They can be noise (for example in "
            "corners or on clutter) or real change. Look at them in CloudCompare "
            "before you trust the flagged points.")
        if "p99_abs" in estimate:
            lines.append(
                f"For Stage 6: 95 % of the distances are smaller than "
                f"{estimate['p95_abs'] * 100:.2f} cm and 99 % are smaller than "
                f"{estimate['p99_abs'] * 100:.2f} cm. A Stage 6 threshold of 2 to 3 "
                f"times the spread ({sigma * 200:.2f} to {sigma * 300:.2f} cm) can flag "
                "many more points than you expect. Use 'Check Threshold Against the "
                "Diff File' in Stage 6 before you run it.")
    lines.append(
        "The spread includes some surface roughness, so it is an upper estimate of "
        "the registration error.")
    return "\n".join(lines)


def build_surface_command(surface_script, input_ply, output_ply, method="poisson",
                           depth=None, density_trim_percentile=None, ball_radii=None,
                           carry_field=None, pipeline=None):
    """
    Delegates to surface_reconstruction.py, converting Stage 6's
    classified change-highlight cloud into a triangle mesh - solid
    surfaces read much better than raw points in Omniverse, and meshing
    also unlocks surface-area/volume quantification of the flagged
    damage. See surface_reconstruction.py's own docstring for how to pick
    between the two methods.

    method: "poisson" (smooth continuous surface, good for room shells/
    walls/floors, tends to over-smooth cluttered scenes) or
    "ball_pivoting" (stays closer to the actual points, generally better
    for machinery/cluttered/thin geometry).

    carry_field: name of a per-vertex scalar field in the input (e.g.
    "M3C2 distance") to carry through onto the reconstructed mesh's
    vertices, so the mesh can still be colored by change magnitude in USD.
    Requires scipy on the machine running surface_reconstruction.py.

    pipeline: optional project_manager.PipelineHandle for a DIFF pipeline.
    input_ply is always used exactly as given (see build_slam_command()'s
    docstring for the full explanation) - only start_stage() tracking
    happens here now.
    """
    if pipeline is not None:
        project_manager.start_stage(pipeline, "surface", params={
            "method": method,
            "depth": depth,
            "density_trim_percentile": density_trim_percentile,
            "ball_radii": ball_radii,
            "carry_field": carry_field,
        })

    cmd = [sys.executable, str(surface_script),
           "--input", str(input_ply),
           "--output", str(output_ply),
           "--method", str(method)]
    if depth is not None:
        cmd += ["--depth", str(depth)]
    if density_trim_percentile is not None:
        cmd += ["--density-trim-percentile", str(density_trim_percentile)]
    if ball_radii:
        cmd += ["--ball-radii", str(ball_radii)]
    if carry_field:
        cmd += ["--carry-field", str(carry_field)]
    return cmd


# ---------------------------------------------------------------------------
# Stage 7: Surface - Blender method (Geometry Nodes recipe in a .blend file)
# ---------------------------------------------------------------------------

BLENDER_ENV_VARS = ("DELTA_BLENDER", "BLENDER_EXE")


def _blender_version_key(path):
    """Sort key for an installed Blender, from its folder name - e.g.
    '...\\Blender Foundation\\Blender 5.2\\blender.exe' -> (5, 2). Unknown
    -> (0,), so a named version always wins over an unnamed folder."""
    match = re.search(r"Blender[ _-]?(\d+)\.(\d+)", str(path), re.IGNORECASE)
    return (int(match.group(1)), int(match.group(2))) if match else (0,)


def find_blender_executable():
    """
    Returns the path of a Blender program to run Stage 7's Blender method
    with, or None if none is found. Never raises - this only fills in a
    default in the Stage 7 panel; the user can always browse to
    blender.exe instead.

    Search order:
      1. The DELTA_BLENDER or BLENDER_EXE environment variable (a path to
         the program), if it points at an existing file.
      2. `blender` on PATH.
      3. Standard install folders: on Windows, every
         <Program Files>\\Blender Foundation\\Blender <version>\\blender.exe
         (the NEWEST version wins - the D.E.L.T.A. .blend file is saved by
         Blender 5.2), then the Steam install folder; on macOS, /Applications/Blender.app; on Linux,
         /usr/bin/blender and /snap/bin/blender.
    """
    for name in BLENDER_ENV_VARS:
        value = os.environ.get(name, "").strip().strip('"')
        if value and Path(value).is_file():
            return str(Path(value))

    on_path = shutil.which("blender")
    if on_path:
        return on_path

    candidates = []
    if sys.platform.startswith("win"):
        roots = {os.environ.get(v) for v in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")}
        roots.discard(None)
        roots.add(r"C:\Program Files")
        for root in roots:
            candidates += glob.glob(os.path.join(root, "Blender Foundation", "Blender*",
                                                 "blender.exe"))
            candidates.append(os.path.join(root, "Steam", "steamapps", "common", "Blender",
                                           "blender.exe"))
    elif sys.platform == "darwin":
        candidates += glob.glob("/Applications/Blender*.app/Contents/MacOS/Blender")
    else:
        candidates += [p for p in ("/usr/bin/blender", "/usr/local/bin/blender",
                                   "/snap/bin/blender") if os.path.isfile(p)]
    candidates = [c for c in candidates if os.path.isfile(c)]
    if not candidates:
        return None
    return max(candidates, key=_blender_version_key)


# Vertex properties that Blender's PLY importer does not keep under their
# own names (position, normals, colours, texture coordinates) - they
# cannot be carried by blender_surface.py.
PLY_NON_CARRY_FIELDS = {"x", "y", "z", "nx", "ny", "nz", "red", "green", "blue", "alpha",
                        "r", "g", "b", "a", "s", "t", "u", "v"}


def split_field_names(value):
    """None, 'a', 'a, b' or ['a', 'b'] -> ['a', 'b'] (unique, in order)."""
    if not value:
        return []
    parts = value if isinstance(value, (list, tuple)) else str(value).split(",")
    names = []
    for part in parts:
        name = str(part).strip()
        if name and name not in names:
            names.append(name)
    return names


def read_ply_vertex_fields(path):
    """
    Reads only the header of a .ply file (ASCII or binary) and returns its
    vertex properties as [(name, ply_type), ...], in file order. Returns
    None if the file is missing or is not a readable PLY. Fast for any
    file size - it stops at 'end_header'.
    """
    try:
        with open(path, "rb") as f:
            if f.readline().strip() != b"ply":
                return None
            fields = []
            in_vertex = False
            for _ in range(10000):
                line = f.readline()
                if not line:
                    return None
                words = line.decode("ascii", errors="replace").split()
                if not words:
                    continue
                if words[0] == "end_header":
                    return fields
                if words[0] == "element":
                    in_vertex = len(words) > 1 and words[1] == "vertex"
                elif words[0] == "property" and in_vertex and len(words) >= 3 \
                        and words[1] != "list":
                    fields.append((words[-1], words[1]))
    except OSError:
        return None
    return None


def carryable_ply_fields(path):
    """The vertex fields of a .ply that Stage 7 can carry onto the mesh
    (every extra vertex property - not x/y/z, normals, colours or texture
    coordinates). None if the header cannot be read."""
    fields = read_ply_vertex_fields(path)
    if fields is None:
        return None
    return [name for name, _type in fields if name.lower() not in PLY_NON_CARRY_FIELDS]


def build_blender_surface_command(blender_exe, blend_file, blender_script, input_ply,
                                  output_ply, object_name=None, overrides=None,
                                  carry_field=None, pipeline=None):
    """
    Stage 7's Blender method: runs scripts/blender_surface.py inside
    Blender, in the background (no Blender window), against a .blend file
    that holds the surfacing recipe as a Geometry Nodes modifier
    (configs/ExtraDownsampling.blend: stray-point removal, Points to
    Volume, Volume to Mesh). See blender_surface.py's docstring for what
    happens inside Blender. The output is a triangle-mesh .ply in the same
    format surface_reconstruction.py writes, so Stage 8 (Export) reads it
    unchanged.

    overrides: optional {"Panel/Name": value} for the modifier's inputs,
    e.g. {"Mesh/Voxel Size": 0.025}. Lengths in METRES (the GUI shows cm
    and converts - see qt_stage_base.LengthFieldRef). An input not given
    keeps the value saved in the .blend file. Passed as one
    --set "Panel/Name=value" argument each.

    carry_field: one field name, a comma-separated string, or a list of
    names (update 9) - per-point fields of input_ply (usually
    scalar_M3C2_distance, and cluster_id for a Classify output) copied
    onto the mesh vertices by nearest original point. One --carry-field
    argument per field. Recorded as params "carry_fields" (list) and
    "carry_field" (the same names joined with ", ", for older readers).
    Use carryable_ply_fields() to list what an input offers.

    object_name: optional - which object in the .blend holds the
    modifier. Default (None): the only object that has one.

    Argument order matters for Blender: --python-exit-code must come
    BEFORE --python, or a Python error inside Blender still exits 0 and
    the app would record a failed run as complete. --factory-startup
    ignores the user's own Blender preferences and add-ons, so every PC
    runs the recipe the same way. The script's own arguments follow "--".

    pipeline: optional project_manager.PipelineHandle for a DIFF pipeline
    - start_stage("surface") tracking only, same as build_surface_command().
    """
    overrides = dict(overrides or {})
    carry_fields = split_field_names(carry_field)
    if pipeline is not None:
        project_manager.start_stage(pipeline, "surface", params={
            "method": "blender",
            "blender": str(blender_exe),
            "blend_file": str(blend_file),
            "object": object_name,
            "overrides": overrides,
            "carry_field": ", ".join(carry_fields) or None,
            "carry_fields": carry_fields,
        })

    cmd = [str(blender_exe), "--background", "--factory-startup",
           "--python-exit-code", "1",
           "--python", str(blender_script),
           "--",
           "--blend", str(blend_file),
           "--input", str(input_ply),
           "--output", str(output_ply)]
    if object_name:
        cmd += ["--object", str(object_name)]
    for path, value in overrides.items():
        cmd += ["--set", f"{path}={value}"]
    for name in carry_fields:
        cmd += ["--carry-field", name]
    return cmd


# ---------------------------------------------------------------------------
# Stage 8: Export (USD) - delegates to an external script
# ---------------------------------------------------------------------------

def build_damage_detail_command(detail_script, flagged_ply, comparison_ply, output_ply,
                                radius):
    """
    Builds the extract_damage_detail.py command (Stage 8's optional
    "Extract Damage Detail" helper step). Not a tracked project stage -
    no pipeline parameter, no start_stage()/finish_stage(): the result is
    an optional extra INPUT for Stage 8 (its "Damage detail .ply" field),
    not a stage output of its own. The caller still picks where it goes
    (in project mode, the Qt app names it inside the diff's own export
    folder via project_manager.get_output_path(pipeline, "export", ".ply")).

    CLI flags confirmed from extract_damage_detail.py's own argparse:
    --flagged, --comparison, --output, --radius (all required).
    """
    return [sys.executable, str(detail_script),
            "--flagged", str(flagged_ply),
            "--comparison", str(comparison_ply),
            "--output", str(output_ply),
            "--radius", str(float(radius))]


def build_export_command(export_script, baseline_ply, change_ply, output_usd,
                          package_usdz=False, detail_ply=None, voxel_size=None,
                          clusters_json=None, surfaces_ply=None, flagged_ply=None,
                          pipeline=None):
    """
    Calls out to a separate Python script that does the actual PLY -> USD
    conversion (builds /World/Compartment/Baseline and .../ChangeHighlight
    prims, sets vertex color interpolation, Z-up, etc.) That script isn't
    included here - point --export-script at wherever yours lives.
    Accepts either point clouds or meshes for either input - usd_export.py
    auto-detects which it was given (a mesh most naturally comes from
    Stage 7/surface_reconstruction.py now that it's a wired stage).

    detail_ply: optional output from extract_damage_detail.py - real
    comparison-cloud geometry near flagged locations, added as a third
    /World/Compartment/DamageDetail layer alongside the abstract
    magnitude-only ChangeHighlight (which sits at baseline positions,
    since M3C2's core points are baseline-sourced). Not a tracked project
    stage (PROJECT_SCHEMA_v2.md's diff stage order has no entry for it) -
    always passed explicitly, in both manual and project mode.

    voxel_size: optional - downsamples point cloud layers (not meshes)
    before writing, via voxel-grid binning. Reduces point count /
    processing load in viewers like Isaac Sim, without meaningfully
    losing detail as long as it's kept smaller than the smallest feature
    you care about seeing - removes redundant near-duplicate points from
    overlapping scan passes, not real geometry. Off by default.

    clusters_json: optional - m3c2_classify.py's <name>.clusters.json (the
    per-damage-site summary Stage 6 writes next to its output). Passed as
    --clusters; usd_export.py adds a /World/Compartment/DamageSites group
    with one marker per site. Not a tracked stage output - the Stage 8
    panel pre-fills it from this diff's recorded Classify output.

    surfaces_ply: optional, only used with clusters_json (update 6) - a
    Stage 4 (Segment) cloud of this diff's reference scan
    (<name>_classified.ply or <name>_envelope_filtered.ply). Passed as
    --surfaces; usd_export.py labels each damage site with the surface it
    sits on (wall_2, floor, ...) from the manifest.json beside that file.
    The Stage 8 panel pre-fills it with default_surfaces_for_diff().

    flagged_ply: optional (update 7) - a Stage 6 (Classify) output.
    Passed as --flagged; usd_export.py adds /World/Compartment/
    FlaggedPoints with only its flagged points (cluster_id >= 0, else
    classified = 1). For a Classify run with keep_all: Stage 7 meshes all
    points for change_ply, and this layer shows only the damage. The
    Stage 8 panel pre-fills it with default_flagged_for_diff().

    package_usdz: adds --usdz, which (in usd_export.py) also packages the
    result as a .usdz alongside the .usd - needed for most web/AR/mobile
    USD viewers, which often reject a raw .usd even when it's valid.

    pipeline: optional project_manager.PipelineHandle for a DIFF pipeline.
    Both baseline_ply and change_ply are always used exactly as given
    (PROJECT_INPUT_PICKER_PLAN.md Section 5.5). The caller resolves
    change_ply from this diff's own earlier stages (Diff/Classify/Surface)
    and baseline_ply from the PROJECT's baseline cleanup/segment output
    (project_manager.list_side_candidates(project, "baseline")) - the
    exported scene's static environment layer stays the same regardless
    of which diff produced the change highlight, since a diff's own
    `reference` might be an earlier scan rather than the baseline itself.
    """
    if pipeline is not None:
        project_manager.start_stage(pipeline, "export", params={
            "package_usdz": package_usdz,
            "voxel_size": voxel_size,
            "detail_ply": str(detail_ply) if detail_ply else None,
            "clusters_json": str(clusters_json) if clusters_json else None,
            "surfaces_ply": str(surfaces_ply) if (surfaces_ply and clusters_json) else None,
            "flagged_ply": str(flagged_ply) if flagged_ply else None,
        })

    cmd = [sys.executable, str(export_script),
           "--baseline", str(baseline_ply),
           "--change", str(change_ply),
           "--output", str(output_usd)]
    if detail_ply:
        cmd += ["--detail", str(detail_ply)]
    if flagged_ply:
        cmd += ["--flagged", str(flagged_ply)]
    if clusters_json:
        cmd += ["--clusters", str(clusters_json)]
        if surfaces_ply:
            cmd += ["--surfaces", str(surfaces_ply)]
    if voxel_size:
        cmd += ["--voxel-size", str(voxel_size)]
    if package_usdz:
        cmd.append("--usdz")
    return cmd


def is_segment_cloud(path):
    """True if path is a Stage 4 (Segment) cloud with a 'classification'
    field: <name>_classified.ply or <name>_envelope_filtered.ply."""
    name = Path(str(path)).name
    return name.endswith("classified.ply") or name.endswith("envelope_filtered.ply")


def default_flagged_for_diff(pipeline):
    """
    The Classify output to show as flagged points in Stage 8
    (usd_export.py --flagged), or None. Update 7.

    Only when this diff's recorded Classify run used keep_all ('Keep all
    points') and its output file exists: Stage 7 then meshes every point,
    so the flagged points need their own layer. After a Classify run
    without keep_all, the Stage 7 mesh already shows only the damage.
    Never raises.
    """
    if pipeline is None or getattr(pipeline, "kind", None) != "diff":
        return None
    try:
        classify = pipeline.entry["stages"].get("classify", {}) or {}
        if not (classify.get("params") or {}).get("keep_all"):
            return None
        output = classify.get("output")
        if not output:
            return None
        path = project_manager.get_absolute_path(pipeline.project, output)
        return str(path) if Path(path).is_file() else None
    except Exception:
        return None


def default_surfaces_for_diff(pipeline):
    """
    The Stage 4 (Segment) cloud to label this diff's damage sites with
    (usd_export.py --surfaces), or None. Update 6.

    First choice: the reference cloud that this diff's Stage 5 run
    actually used (recorded as stages.diff.params.reference_input), when
    it is a Segment cloud and the file still exists. M3C2's core points
    are that cloud's own points, so the damage sites sit on its surfaces.
    Second choice: project_manager.get_diff_inputs()'s reference side,
    when that is a Segment output. Never raises.
    """
    if pipeline is None or getattr(pipeline, "kind", None) != "diff":
        return None
    try:
        recorded = pipeline.entry["stages"].get("diff", {}).get("params", {}) \
            .get("reference_input")
        if recorded:
            path = project_manager.get_absolute_path(pipeline.project, recorded)
            if is_segment_cloud(path) and Path(path).is_file():
                return str(path)
        inputs = project_manager.get_diff_inputs(pipeline)
        if inputs["reference_source_stage"] == "segment" \
                and Path(inputs["reference_path"]).is_file():
            return inputs["reference_path"]
    except Exception:
        return None
    return None
