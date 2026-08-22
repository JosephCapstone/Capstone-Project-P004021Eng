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

import json
import os
import re
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


def run_streaming(cmd, on_line, on_done, cwd=None):
    """
    Run `cmd` in a background thread. Calls on_line(str) for each line of
    combined stdout/stderr as it arrives, and on_done(returncode) once the
    process exits. Safe to call from a GUI main thread - it does not block.
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
            process = subprocess.Popen(
                cmd_to_run,
                cwd=cwd,
                shell=use_shell,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
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
                        deskew_method="constant_velocity", pipeline=None):
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
            "voxel_size": voxel_size,
            "deskew_method": deskew_method,
            "meta": str(meta) if meta else None,
        })

    cmd = ["ouster-cli", "source"]
    if meta:
        cmd += ["--meta", str(meta)]
    cmd += [str(source), "slam", "--voxel-size", str(voxel_size)]
    if deskew_method:
        cmd += ["--deskew-method", str(deskew_method)]
    if visualize:
        cmd += ["viz"]
    cmd += ["save", str(output_ply)]
    return cmd


def read_kiss_icp_voxel_size(config_path):
    """
    Reads `mapping.voxel_size` from a kiss-icp config YAML (see
    kiss_icp_config_indoor.yaml's own structure - a top-level `mapping`
    section holding `voxel_size` among other fields). slam_kiss_icp.py
    has no CLI flag exposing this on its own (only --config and
    --max-range, per its own docstring) - voxel size otherwise only
    lives inside whatever config file is given, invisible anywhere else,
    which is what makes it hard for Stage 2 (Level)'s distance-threshold
    guidance to know what Stage 1 actually used when KISS-ICP was the
    backend (Ouster CLI's own voxel size is a direct, always-visible
    dialog field; KISS-ICP's is not, unless read back out of its config
    like this).

    Returns None if `config_path` is falsy, the file doesn't exist,
    isn't valid YAML, or has no `mapping.voxel_size` field (e.g. a
    config that leaves it at kiss-icp's own null/auto-derived default -
    a real, valid state, not an error). Never raises - this is read for
    informational display and for recording what was actually used, not
    to gate anything.
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
    mapping = (data or {}).get("mapping", {}) or {}
    value = mapping.get("voxel_size")
    return float(value) if isinstance(value, (int, float)) else None


def build_kiss_icp_slam_command(kiss_icp_script, source, output_ply, config=None,
                                 dataloader=None, topic=None, meta=None,
                                 voxel_size=None, pipeline=None):
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

    if pipeline is not None:
        project_manager.start_stage(pipeline, "slam", params={
            "backend": "kiss_icp",
            "config": str(config) if config else None,
            "dataloader": dataloader,
            "topic": topic,
            "voxel_size": effective_voxel_size,
            "voxel_size_overridden": voxel_size is not None,
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
        actually flagged outside the envelope), "n_outside_envelope"
        (int, only present if envelope_filter was on and an envelope was
        actually detected to filter against), "classification_ids"
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
    if manifest.get("envelope_filter_applied"):
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

def build_cleanup_command(input_ply, output_ply, sor_neighbors=6, sor_std_dev=1.0,
                           align_to_ply=None, log_file=None, pipeline=None):
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
    """
    if pipeline is not None:
        project_manager.start_stage(pipeline, "cleanup", params={
            "sor_neighbors": sor_neighbors,
            "sor_std_dev": sor_std_dev,
            "align_to": str(align_to_ply) if align_to_ply else None,
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
        project_manager.start_stage(pipeline, "diff", params={
            "m3c2_params_file": str(m3c2_params_file),
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

def build_classify_command(classify_script, input_ply, output_ply, threshold, keep_all=False,
                            cluster=True, cluster_method="dbscan", cluster_eps=0.05,
                            cluster_min_samples=4, min_cluster_size=4, pipeline=None):
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

    m3c2_classify.py writes its Step D per-cluster summary as a sidecar
    file next to output_ply (same name, '.clusters.json' extension) when
    clustering is enabled - see resolve_classify_output(), the companion
    function that reads it back after the subprocess finishes, mirroring
    resolve_segment_output()'s manifest-absorption pattern (Section 3.3).

    pipeline: optional project_manager.PipelineHandle for a DIFF pipeline.
    input_ply is always used exactly as given (see build_slam_command()'s
    docstring for the full explanation) - only start_stage() tracking
    happens here now.
    """
    if pipeline is not None:
        project_manager.start_stage(pipeline, "classify", params={
            "threshold": threshold,
            "keep_all": keep_all,
            "cluster": cluster,
            "cluster_method": cluster_method if cluster else None,
            "cluster_eps": cluster_eps if (cluster and cluster_method == "dbscan") else None,
            "cluster_min_samples": cluster_min_samples if cluster else None,
            "min_cluster_size": min_cluster_size if cluster else None,
        })

    cmd = [sys.executable, str(classify_script),
           "--input", str(input_ply),
           "--output", str(output_ply),
           "--threshold", str(threshold)]
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
    return cmd


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
    """
    summary_path = Path(output_ply).with_suffix(".clusters.json")
    if not summary_path.exists():
        return {}
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    extra_fields = {}
    for key in ("n_flagged", "n_confirmed", "n_noise"):
        if key in summary:
            extra_fields[key] = summary[key]
    clusters = summary.get("clusters")
    if clusters:
        extra_fields["clusters"] = clusters
    return extra_fields


# ---------------------------------------------------------------------------
# Stage 7: Surface (Open3D - reconstructs a mesh from a point cloud)
# ---------------------------------------------------------------------------

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
# Stage 8: Export (USD) - delegates to an external script
# ---------------------------------------------------------------------------

def build_export_command(export_script, baseline_ply, change_ply, output_usd,
                          package_usdz=False, detail_ply=None, voxel_size=None,
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
        })

    cmd = [sys.executable, str(export_script),
           "--baseline", str(baseline_ply),
           "--change", str(change_ply),
           "--output", str(output_usd)]
    if detail_ply:
        cmd += ["--detail", str(detail_ply)]
    if voxel_size:
        cmd += ["--voxel-size", str(voxel_size)]
    if package_usdz:
        cmd.append("--usdz")
    return cmd
