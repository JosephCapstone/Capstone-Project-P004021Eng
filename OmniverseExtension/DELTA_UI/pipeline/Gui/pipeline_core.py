"""Build stage commands and stream processing results."""

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
    entry["history"].append(
        {
            "path": str(ply_path),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "note": note,
        }
    )
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


def generate_m3c2_params_file(
    output_path, normal_scale, search_scale, search_depth, registration_error
):
    """Writes a CloudCompare M3C2 params .txt, based on a confirmed-working reference file - only the four values known to matter for this workflow are parameterized; everything else is copied verbatim from that reference."""
    content = M3C2_PARAMS_TEMPLATE.format(
        normal_scale=normal_scale,
        search_scale=search_scale,
        search_depth=search_depth,
        registration_error=registration_error,
    )
    Path(output_path).write_text(content, encoding="utf-8")
    return output_path


def read_m3c2_normal_scale(params_file):
    """Reads the NormalScale= value out of a CloudCompare M3C2 params .txt (the format generate_m3c2_params_file() writes, and CloudCompare's own GUI saves)."""
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
    """Reads the SearchScale= value (the M3C2 projection scale, the DIAMETER of the projection cylinder) out of a CloudCompare M3C2 params .txt."""
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
    """Run `cmd` in a background thread."""

    def worker():
        try:
            # Execute argument lists directly so paths and Python snippets retain their quoting.
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            popen_kwargs = (
                {"creationflags": subprocess.CREATE_NO_WINDOW}
                if sys.platform.startswith("win")
                else {"start_new_session": True}
            )
            process = subprocess.Popen(
                cmd,
                cwd=cwd,
                shell=False,
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
    """Stops a process started by run_streaming() AND every process it started."""
    if process is None or process.poll() is not None:
        return False
    if sys.platform.startswith("win"):
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        import signal

        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            process.kill()
    return True


def annotate_log_file(log_path, stage_title, cmd):
    """Rewrites a CloudCompare -LOG_FILE output in place, wrapping the raw console dump with clear section headers (stage name, timestamp, the exact command run) so RMS / fitness / stats lines are easier to find than in an."""
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
    """Extracts the ICP registration RMS from a CloudCompare -LOG_FILE output, matching a line like 'RMS: 0.147367'."""
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
    _rms_sidecar_path(ply_path).write_text(
        json.dumps(sidecar, indent=2), encoding="utf-8"
    )
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


def inspect_rosbag_topics(bag_folder):
    """Reads a ROS2 bag folder's metadata.yaml and reports what it finds."""
    try:
        import yaml
    except ImportError:
        raise RuntimeError(
            "Reading a ROS2 bag's metadata.yaml needs PyYAML. Run: pip install pyyaml"
        )

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
        topics.append(
            {
                "name": meta.get("name"),
                "type": meta.get("type"),
                "count": entry.get("message_count"),
            }
        )

    pointcloud2_topic = next(
        (t["name"] for t in topics if t["type"] == "sensor_msgs/msg/PointCloud2"), None
    )

    packet_topics = [
        t for t in topics if t["type"] == "ouster_sensor_msgs/msg/PacketMsg"
    ]
    raw_lidar = next(
        (t for t in packet_topics if "lidar" in (t["name"] or "").lower()), None
    )
    if raw_lidar is None and len(packet_topics) == 1:
        raw_lidar = packet_topics[0]
    raw_imu = next(
        (t for t in packet_topics if "imu" in (t["name"] or "").lower()), None
    )

    metadata_topic = next(
        (
            t["name"]
            for t in topics
            if t["type"] == "std_msgs/msg/String"
            and "metadata" in (t["name"] or "").lower()
        ),
        None,
    )

    return {
        "topics": topics,
        "has_pointcloud2": pointcloud2_topic is not None,
        "pointcloud2_topic": pointcloud2_topic,
        "raw_lidar_topic": raw_lidar["name"] if raw_lidar else None,
        "raw_lidar_count": raw_lidar["count"] if raw_lidar else None,
        "raw_imu_topic": raw_imu["name"] if raw_imu else None,
        "metadata_topic": metadata_topic,
    }


def build_decode_command(
    decode_script,
    bag_folder,
    output_bag,
    lidar_topic=None,
    imu_topic=None,
    metadata_topic=None,
    points_topic=None,
):
    """
    Delegates to decode_raw_packets.py, converting a ROS2 bag of raw
    Ouster packets into a new bag carrying decoded sensor_msgs/msg/
    PointCloud2 messages - see that script's own docstring for why this
    exists and what it does not yet do (IMU decoding).
    """
    cmd = [
        sys.executable,
        str(decode_script),
        "--input",
        str(bag_folder),
        "--output",
        str(output_bag),
    ]
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


def finish_stage(
    pipeline,
    stage_name,
    output_path,
    success,
    error_message=None,
    extra_fields=None,
    log_path=None,
):
    """Reports a stage's actual completion or failure to the project, once the subprocess launched from a build_X_command()'s returned cmd has actually finished running."""
    if pipeline is None:
        return
    if success:
        project_manager.complete_stage(
            pipeline,
            stage_name,
            output_path,
            extra_fields=extra_fields,
            log_path=log_path,
        )
    else:
        project_manager.fail_stage(pipeline, stage_name, error_message=error_message)


# ---------------------------------------------------------------------------
# Stage 1: SLAM (ouster-cli)
# ---------------------------------------------------------------------------


def build_slam_command(
    source,
    voxel_size,
    output_ply,
    meta=None,
    visualize=False,
    deskew_method="constant_velocity",
    map_max_range=None,
    pipeline=None,
):
    """source: a .pcap file, an OSF file, a ROS1 .bag file, or a ROS2 bag folder (containing .db3 + metadata.yaml) - ouster-cli's "source" argument auto-detects the type from what it's given, so the command shape is."""
    if pipeline is not None:
        project_manager.start_stage(
            pipeline,
            "slam",
            params={
                "backend": "ouster",
                "voxel_size": voxel_size,
                "deskew_method": deskew_method,
                "meta": str(meta) if meta else None,
                "map_max_range": map_max_range,
            },
        )

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
    """Reads `<section>.<key>` from a kiss-icp config YAML (see kiss_icp_config_indoor.yaml's own structure - top-level `data` and `mapping` sections)."""
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
    """Reads `mapping.voxel_size` from a kiss-icp config YAML."""
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
    """Reads `data.max_range` from a kiss-icp config YAML - backs the "This config's own max_range" GUI label."""
    return _read_kiss_icp_config_value(config_path, "data", "max_range")


def build_kiss_icp_slam_command(
    kiss_icp_script,
    source,
    output_ply,
    config=None,
    dataloader=None,
    topic=None,
    meta=None,
    voxel_size=None,
    min_range=None,
    map_max_range=None,
    map_max_horizontal=None,
    map_max_vertical=None,
    pipeline=None,
):
    """Delegates to slam_kiss_icp.py - a second, genuinely different SLAM backend from ouster-cli's own bundled kiss-icp (same underlying algorithm, different entry point/config/input handling), useful for comparing output."""
    effective_voxel_size = (
        voxel_size if voxel_size is not None else read_kiss_icp_voxel_size(config)
    )
    effective_min_range = (
        min_range if min_range is not None else read_kiss_icp_min_range(config)
    )

    if pipeline is not None:
        project_manager.start_stage(
            pipeline,
            "slam",
            params={
                "backend": "kiss_icp",
                "config": str(config) if config else None,
                "dataloader": dataloader,
                "topic": topic,
                "voxel_size": effective_voxel_size,
                "voxel_size_overridden": voxel_size is not None,
                "min_range": effective_min_range,
                "min_range_overridden": min_range is not None,
                "max_range": (
                    map_max_range
                    if map_max_range is not None
                    else read_kiss_icp_max_range(config)
                ),
                "max_range_overridden": map_max_range is not None,
                "config_max_range": read_kiss_icp_max_range(config),
                "map_max_range": map_max_range,
                "map_max_horizontal": map_max_horizontal,
                "map_max_vertical": map_max_vertical,
            },
        )

    cmd = [
        sys.executable,
        str(kiss_icp_script),
        "--input",
        str(source),
        "--output",
        str(output_ply),
    ]
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


def build_level_command(
    level_script,
    input_ply,
    output_ply,
    distance_threshold=None,
    max_planes=None,
    min_inlier_fraction=None,
    horizontal_threshold=None,
    pipeline=None,
):
    """Delegates to level_cloud.py."""
    if pipeline is not None:
        project_manager.start_stage(
            pipeline,
            "level",
            params={
                "distance_threshold": distance_threshold,
                "max_planes": max_planes,
                "min_inlier_fraction": min_inlier_fraction,
                "horizontal_threshold": horizontal_threshold,
            },
        )

    cmd = [
        sys.executable,
        str(level_script),
        "--input",
        str(input_ply),
        "--output",
        str(output_ply),
    ]
    if distance_threshold is not None:
        cmd += ["--distance-threshold", str(distance_threshold)]
    if max_planes is not None:
        cmd += ["--max-planes", str(max_planes)]
    if min_inlier_fraction is not None:
        cmd += ["--min-inlier-fraction", str(min_inlier_fraction)]
    if horizontal_threshold is not None:
        cmd += ["--horizontal-threshold", str(horizontal_threshold)]
    return cmd


def build_segment_command(
    segment_script,
    input_ply,
    output_dir,
    distance_threshold=0.05,
    max_planes=20,
    horizontal_threshold=0.7,
    max_horizontal_z_span=0.3,
    min_inlier_fraction=0.003,
    cluster_filter=True,
    cluster_eps=0.5,
    cluster_min_points=20,
    merge_coplanar=True,
    merge_normal_cos=0.98,
    merge_distance=0.1,
    write_separate_surfaces=False,
    envelope_filter=True,
    envelope_margin=0.15,
    write_envelope_filtered=False,
    pipeline=None,
):
    """Delegates to segment_planes.py, classifying input_ply's points into floor/ceiling/wall_N/unclassified (a height + orientation heuristic - see that script's own docstring for the full explanation, including its."""
    if pipeline is not None:
        project_manager.start_stage(
            pipeline,
            "segment",
            params={
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
            },
        )

    cmd = [
        sys.executable,
        str(segment_script),
        "--input",
        str(input_ply),
        "--output-dir",
        str(output_dir),
        "--distance-threshold",
        str(distance_threshold),
        "--max-planes",
        str(max_planes),
        "--horizontal-threshold",
        str(horizontal_threshold),
        "--max-horizontal-z-span",
        str(max_horizontal_z_span),
        "--min-inlier-fraction",
        str(min_inlier_fraction),
    ]
    if cluster_filter:
        cmd += [
            "--cluster-eps",
            str(cluster_eps),
            "--cluster-min-points",
            str(cluster_min_points),
        ]
    else:
        cmd.append("--no-cluster-filter")
    if merge_coplanar:
        cmd += [
            "--merge-normal-cos",
            str(merge_normal_cos),
            "--merge-distance",
            str(merge_distance),
        ]
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
    """Reads segment_planes.py's manifest.json from output_dir, once a build_segment_command() run has finished - this is what turns that file's content into the record a caller can pass straight into finish_stage()'s."""
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
        raise ValueError(
            "ICP final overlap must be a whole number from "
            f"{ICP_OVERLAP_MIN} to {ICP_OVERLAP_MAX} (percent), "
            "or blank."
        )
    if (
        value != value
        or value in (float("inf"), float("-inf"))
        or value != int(value)
        or not (ICP_OVERLAP_MIN <= value <= ICP_OVERLAP_MAX)
    ):
        raise ValueError(
            "ICP final overlap must be a whole number from "
            f"{ICP_OVERLAP_MIN} to {ICP_OVERLAP_MAX} (percent), "
            "or blank."
        )
    return int(value)


def build_cleanup_command(
    input_ply,
    output_ply,
    sor_neighbors=6,
    sor_std_dev=1.0,
    align_to_ply=None,
    log_file=None,
    pipeline=None,
    icp_overlap=None,
):
    """If align_to_ply is given, runs ICP to register input_ply onto it after outlier removal (input_ply is treated as the moving cloud)."""
    icp_overlap = parse_icp_overlap(icp_overlap)
    if pipeline is not None:
        project_manager.start_stage(
            pipeline,
            "cleanup",
            params={
                "sor_neighbors": sor_neighbors,
                "sor_std_dev": sor_std_dev,
                "align_to": str(align_to_ply) if align_to_ply else None,
                "icp_overlap": icp_overlap if align_to_ply else None,
            },
        )

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
    """Finds whatever new .ply file(s) CloudCompare actually created during a Stage 3 run and renames the one that's almost certainly the real cleaned/aligned result (its filename starts with the input cloud's own stem,."""
    new_files = find_new_ply_files(directory, existing_before)
    if not new_files:
        return (
            None,
            [],
            "No new .ply files appeared after the run - it may have failed silently.",
        )

    input_stem = Path(input_ply).stem
    candidates = [f for f in new_files if f.stem.startswith(input_stem)]

    if candidates:
        chosen = candidates[-1]  # most recently modified match
    elif len(new_files) == 1:
        chosen = new_files[0]
    else:
        names = ", ".join(f.name for f in new_files)
        return (
            None,
            new_files,
            (
                f"Found {len(new_files)} new .ply file(s) but couldn't confidently tell "
                f"which is the cleaned/aligned result: {names}. Check them manually."
            ),
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


def build_diff_command(
    baseline_ply, comparison_ply, m3c2_params_file, log_file=None, pipeline=None
):
    """m3c2_params_file: path to a CloudCompare M3C2 params .txt."""
    if pipeline is not None:

        def _project_path(path):
            try:
                return project_manager.to_relative_path(pipeline.project, path)
            except project_manager.ProjectError:
                return str(path)

        project_manager.start_stage(
            pipeline,
            "diff",
            params={
                "m3c2_params_file": str(m3c2_params_file),
                "normal_scale": read_m3c2_normal_scale(m3c2_params_file),
                "reference_input": _project_path(baseline_ply),
                "comparison_input": _project_path(comparison_ply),
            },
        )

    cmd = ["CloudCompare", "-SILENT"]
    if log_file:
        cmd += ["-LOG_FILE", str(log_file)]
    cmd += [
        "-O",
        str(baseline_ply),
        "-O",
        str(comparison_ply),
        "-M3C2",
        str(m3c2_params_file),
        "-C_EXPORT_FMT",
        "PLY",
        "-SAVE_CLOUDS",
    ]
    return cmd


# ---------------------------------------------------------------------------
# Stage 6: Classify (thresholds the M3C2 result into real change vs noise)
# ---------------------------------------------------------------------------


def build_classify_command(
    classify_script,
    input_ply,
    output_ply,
    threshold=None,
    keep_all=False,
    cluster=True,
    cluster_method="dbscan",
    cluster_eps=0.05,
    cluster_min_samples=4,
    min_cluster_size=4,
    use_uncertainty=False,
    pipeline=None,
    find_holes=False,
    hole_min_points=100,
    hole_min_width=0.05,
    projection_radius=0.0,
    interior_point=None,
    hole_value=0.10,
):
    """Delegates to m3c2_classify.py, which drops (or flags) points below the given distance threshold so the change-highlight cloud fed into Stage 7 only shows real change, not the whole M3C2 result."""
    if threshold is None and not use_uncertainty:
        raise ValueError("threshold is required unless use_uncertainty=True.")
    if find_holes and not cluster:
        raise ValueError(
            "Find holes needs clustering. Turn clustering on, or turn "
            "Find holes off."
        )

    if pipeline is not None:
        project_manager.start_stage(
            pipeline,
            "classify",
            params={
                "threshold": threshold,
                "use_uncertainty": use_uncertainty,
                "keep_all": keep_all,
                "cluster": cluster,
                "cluster_method": cluster_method if cluster else None,
                "cluster_eps": (
                    cluster_eps if (cluster and cluster_method == "dbscan") else None
                ),
                "cluster_min_samples": cluster_min_samples if cluster else None,
                "min_cluster_size": min_cluster_size if cluster else None,
                "find_holes": bool(find_holes),
                "hole_min_points": hole_min_points if find_holes else None,
                "hole_min_width": hole_min_width if find_holes else None,
                "projection_radius": projection_radius if find_holes else None,
                "interior_point": (
                    list(interior_point) if (find_holes and interior_point) else None
                ),
                "hole_value": hole_value if find_holes else None,
            },
        )

    cmd = [
        sys.executable,
        str(classify_script),
        "--input",
        str(input_ply),
        "--output",
        str(output_ply),
    ]
    if threshold is not None:
        cmd += ["--threshold", str(threshold)]
    if use_uncertainty:
        cmd.append("--use-uncertainty")
    if keep_all:
        cmd.append("--keep-all")
    if cluster:
        cmd += [
            "--cluster-method",
            str(cluster_method),
            "--cluster-min-samples",
            str(cluster_min_samples),
            "--min-cluster-size",
            str(min_cluster_size),
        ]
        if cluster_method == "dbscan":
            cmd += ["--cluster-eps", str(cluster_eps)]
    else:
        cmd.append("--no-cluster")
    if find_holes:
        cmd += [
            "--find-holes",
            "--min-hole-points",
            str(int(hole_min_points)),
            "--min-hole-width",
            str(hole_min_width),
            "--hole-value",
            str(hole_value),
        ]
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
    """Reads the '*.clusters.json' sidecar m3c2_classify.py writes next to its output when clustering is enabled (Section 3.3's manifest- absorption pattern - the same idea as resolve_segment_output(), but classify's own."""
    summary_path = classify_clusters_path(output_ply)
    if not summary_path.exists():
        return {}
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    extra_fields = {}
    for key in (
        "n_flagged",
        "n_confirmed",
        "n_noise",
        "use_uncertainty",
        "uncertainty_field",
        # Only present when the run used Find holes (m3c2_classify.py
        # --find-holes). n_confirmed / n_noise / n_flagged keep their
        # meaning (measured change only); the points of holes are
        # counted in n_hole_points.
        "find_holes",
        "direction_found",
        "n_added_points",
        "n_removed_points",
        "n_holes",
        "n_hole_points",
        "n_missing",
        "projection_radius",
        # Sites of the type "unmatched": patches with no partner in the
        # other scan (a moved or absent object).
        "find_unmatched",
        "n_unmatched",
        "n_unmatched_points",
        # How the inside of the room was found (m3c2_classify.py
        # --find-holes): "probe", "centroid" or "interior-point".
        "interior_method",
        "n_orient_fallback",
        "orient_disagree_with_centroid",
    ):
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
    """Estimate the registration error from a Stage 5 (M3C2) result."""
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
THRESHOLD_HEAVY_MIN_SHARE = 0.05  # more than 5 % flagged ...
THRESHOLD_HEAVY_RATIO = 5.0  # ... and more than 5 times the bell-curve share


def m3c2_threshold_check(ply_path, threshold, field=M3C2_DISTANCE_FIELD):
    """Compare a Stage 6 distance threshold (metres) with the distances of a Stage 5 (M3C2) result file."""
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
            "points. Use it as a start and look at the result in CloudCompare."
        )
    else:
        lines.append("The tail is not heavy. The threshold is a reasonable start.")
    return "\n".join(lines)


QUICK_REGISTRATION_CORE_POINTS = 10000


def quick_registration_estimate(
    baseline_ply,
    comparison_ply,
    normal_scale,
    search_scale,
    search_depth,
    max_core_points=QUICK_REGISTRATION_CORE_POINTS,
    seed=0,
):
    """Estimate the registration error BEFORE Stage 5 runs, without CloudCompare."""
    try:
        import numpy as np
        from plyfile import PlyData
        from scipy.spatial import cKDTree
    except ImportError as e:
        return None, (
            f"The Python package '{e.name}' is not installed. Install numpy, "
            "scipy and plyfile (see requirements.txt)."
        )
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
        return np.column_stack(
            [np.asarray(vertex[axis], dtype=np.float64) for axis in ("x", "y", "z")]
        )

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
        inside = (np.abs(t) <= half_len) & (
            (v * v).sum(axis=1) - t * t <= radius_cyl**2
        )
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
        t_base = (
            along_normal(base[base_neighbours[i]], core[i], normal)
            if base_neighbours[i]
            else ()
        )
        t_comp = (
            along_normal(comp[comp_neighbours[i]], core[i], normal)
            if comp_neighbours[i]
            else ()
        )
        if len(t_base) == 0 or len(t_comp) == 0:
            continue
        distances[i] = float(np.mean(t_comp) - np.mean(t_base))

    estimate = _spread_statistics(distances)
    if estimate is None:
        return None, (
            "Fewer than 100 core points have a distance. The scans may not "
            "overlap, or the scales are too small for the point spacing. Check "
            "the Normal scale (use 'Check Point Spacing') and the alignment."
        )
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
        return (
            f"CHANGE THE REGISTRATION RMS FROM {entered_rms * 100:.2f} cm "
            f"TO {sigma_cm:.2f} cm AND RUN AGAIN"
        )
    if status == "too_low":
        return (
            f"CHECK THE ALIGNMENT: THE SPREAD ({sigma_cm:.2f} cm) IS LARGER THAN "
            f"THE REGISTRATION RMS ({entered_rms * 100:.2f} cm)"
        )
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
            "as the Registration RMS."
        )
    else:
        status = registration_status(entered_rms, estimate)
        if status == "too_high":
            lines.append(
                f"The Registration RMS ({entered_rms * 100:.2f} cm) is much larger than "
                "this spread. Most of the surface agrees better than the RMS says. "
                "ICP measures the nearest-point distance. M3C2 measures the distance "
                "along the surface normal. The two values can be very different. "
                f"Try {sigma * 100:.2f} cm as the Registration RMS, push 'Generate "
                "Params File...' and run Stage 5 again."
            )
        elif status == "too_low":
            lines.append(
                f"The distances spread more than the Registration RMS "
                f"({entered_rms * 100:.2f} cm) says. The alignment can be poor, or the "
                "surfaces are rough. Check the alignment in CloudCompare before you "
                f"trust small changes. Compare with {sigma * 100:.2f} cm."
            )
        else:
            lines.append(
                f"The Registration RMS ({entered_rms * 100:.2f} cm) agrees with the "
                "spread of the distances."
            )
    if estimate.get("tail_share", 0.0) > 0.02:
        lines.append(
            f"{estimate['tail_share'] * 100:.1f} % of the distances are more than "
            f"{estimate['tail_threshold'] * 100:.1f} cm from the median. The spread "
            "above does not describe these points. They can be noise (for example in "
            "corners or on clutter) or real change. Look at them in CloudCompare "
            "before you trust the flagged points."
        )
        if "p99_abs" in estimate:
            lines.append(
                f"For Stage 6: 95 % of the distances are smaller than "
                f"{estimate['p95_abs'] * 100:.2f} cm and 99 % are smaller than "
                f"{estimate['p99_abs'] * 100:.2f} cm. A Stage 6 threshold of 2 to 3 "
                f"times the spread ({sigma * 200:.2f} to {sigma * 300:.2f} cm) can flag "
                "many more points than you expect. Use 'Check Threshold Against the "
                "Diff File' in Stage 6 before you run it."
            )
    lines.append(
        "The spread includes some surface roughness, so it is an upper estimate of "
        "the registration error."
    )
    return "\n".join(lines)


def build_surface_command(
    surface_script,
    input_ply,
    output_ply,
    method="poisson",
    depth=None,
    density_trim_percentile=None,
    ball_radii=None,
    carry_field=None,
    pipeline=None,
):
    """Delegates to surface_reconstruction.py, converting Stage 6's classified change-highlight cloud into a triangle mesh - solid surfaces read much better than raw points in Omniverse, and meshing also unlocks."""
    if pipeline is not None:
        project_manager.start_stage(
            pipeline,
            "surface",
            params={
                "method": method,
                "depth": depth,
                "density_trim_percentile": density_trim_percentile,
                "ball_radii": ball_radii,
                "carry_field": carry_field,
            },
        )

    cmd = [
        sys.executable,
        str(surface_script),
        "--input",
        str(input_ply),
        "--output",
        str(output_ply),
        "--method",
        str(method),
    ]
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
    """Returns the path of a Blender program to run Stage 7's Blender method with, or None if none is found."""
    for name in BLENDER_ENV_VARS:
        value = os.environ.get(name, "").strip().strip('"')
        if value and Path(value).is_file():
            return str(Path(value))

    on_path = shutil.which("blender")
    if on_path:
        return on_path

    candidates = []
    if sys.platform.startswith("win"):
        roots = {
            os.environ.get(v)
            for v in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")
        }
        roots.discard(None)
        roots.add(r"C:\Program Files")
        for root in roots:
            candidates += glob.glob(
                os.path.join(root, "Blender Foundation", "Blender*", "blender.exe")
            )
            candidates.append(
                os.path.join(
                    root, "Steam", "steamapps", "common", "Blender", "blender.exe"
                )
            )
    elif sys.platform == "darwin":
        candidates += glob.glob("/Applications/Blender*.app/Contents/MacOS/Blender")
    else:
        candidates += [
            p
            for p in ("/usr/bin/blender", "/usr/local/bin/blender", "/snap/bin/blender")
            if os.path.isfile(p)
        ]
    candidates = [c for c in candidates if os.path.isfile(c)]
    if not candidates:
        return None
    return max(candidates, key=_blender_version_key)


# Vertex properties that Blender's PLY importer does not keep under their
# own names (position, normals, colours, texture coordinates) - they
# cannot be carried by blender_surface.py.
PLY_NON_CARRY_FIELDS = {
    "x",
    "y",
    "z",
    "nx",
    "ny",
    "nz",
    "red",
    "green",
    "blue",
    "alpha",
    "r",
    "g",
    "b",
    "a",
    "s",
    "t",
    "u",
    "v",
}


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
                elif (
                    words[0] == "property"
                    and in_vertex
                    and len(words) >= 3
                    and words[1] != "list"
                ):
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


def build_blender_surface_command(
    blender_exe,
    blend_file,
    blender_script,
    input_ply,
    output_ply,
    object_name=None,
    overrides=None,
    carry_field=None,
    pipeline=None,
):
    """Stage 7's Blender method: runs scripts/blender_surface.py inside Blender, in the background (no Blender window), against a .blend file that holds the surfacing recipe as a Geometry Nodes modifier."""
    overrides = dict(overrides or {})
    carry_fields = split_field_names(carry_field)
    if pipeline is not None:
        project_manager.start_stage(
            pipeline,
            "surface",
            params={
                "method": "blender",
                "blender": str(blender_exe),
                "blend_file": str(blend_file),
                "object": object_name,
                "overrides": overrides,
                "carry_field": ", ".join(carry_fields) or None,
                "carry_fields": carry_fields,
            },
        )

    cmd = [
        str(blender_exe),
        "--background",
        "--factory-startup",
        "--python-exit-code",
        "1",
        "--python",
        str(blender_script),
        "--",
        "--blend",
        str(blend_file),
        "--input",
        str(input_ply),
        "--output",
        str(output_ply),
    ]
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


def build_damage_detail_command(
    detail_script, flagged_ply, comparison_ply, output_ply, radius
):
    """Builds the extract_damage_detail.py command (Stage 8's optional "Extract Damage Detail" helper step)."""
    return [
        sys.executable,
        str(detail_script),
        "--flagged",
        str(flagged_ply),
        "--comparison",
        str(comparison_ply),
        "--output",
        str(output_ply),
        "--radius",
        str(float(radius)),
    ]


def build_export_command(
    export_script,
    baseline_ply,
    change_ply,
    output_usd,
    package_usdz=False,
    detail_ply=None,
    voxel_size=None,
    clusters_json=None,
    surfaces_ply=None,
    flagged_ply=None,
    pipeline=None,
):
    """Calls out to a separate Python script that does the actual PLY -> USD conversion (builds /World/Compartment/Baseline and .../ChangeHighlight prims, sets vertex color interpolation, Z-up, etc.) That script isn't."""
    if pipeline is not None:
        project_manager.start_stage(
            pipeline,
            "export",
            params={
                "package_usdz": package_usdz,
                "voxel_size": voxel_size,
                "detail_ply": str(detail_ply) if detail_ply else None,
                "clusters_json": str(clusters_json) if clusters_json else None,
                "surfaces_ply": (
                    str(surfaces_ply) if (surfaces_ply and clusters_json) else None
                ),
                "flagged_ply": str(flagged_ply) if flagged_ply else None,
            },
        )

    cmd = [
        sys.executable,
        str(export_script),
        "--baseline",
        str(baseline_ply),
        "--change",
        str(change_ply),
        "--output",
        str(output_usd),
    ]
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
    """The Classify output to show as flagged points in Stage 8 (usd_export.py --flagged), or None."""
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
    """The Stage 4 (Segment) cloud to label this diff's damage sites with (usd_export.py --surfaces), or None."""
    if pipeline is None or getattr(pipeline, "kind", None) != "diff":
        return None
    try:
        recorded = (
            pipeline.entry["stages"]
            .get("diff", {})
            .get("params", {})
            .get("reference_input")
        )
        if recorded:
            path = project_manager.get_absolute_path(pipeline.project, recorded)
            if is_segment_cloud(path) and Path(path).is_file():
                return str(path)
        inputs = project_manager.get_diff_inputs(pipeline)
        if (
            inputs["reference_source_stage"] == "segment"
            and Path(inputs["reference_path"]).is_file()
        ):
            return inputs["reference_path"]
    except Exception:
        return None
    return None
