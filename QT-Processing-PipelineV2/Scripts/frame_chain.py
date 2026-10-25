#!/usr/bin/env python3
"""
frame_chain.py - Move the poses of the sensor through the frames of the pipeline (Update 30)
==========================================================================================

Stage 1 (SLAM) saves the path of the sensor (--save-poses) and, with QBot topics, the path
of the odometry (--save-odometry). These paths are in the frame of the Stage 1 map. The
later stages move the cloud:

  Stage 2 (Level)    The cloud is rotated about the origin and shifted along Z, so that
                     the floor is level and at Z=0. level_cloud.py writes this 4 x 4
                     matrix to <leveled cloud name>_transform.json (Update 30).
  Stage 3 (Cleanup)  CloudCompare removes points (SOR) and, when an ICP alignment is on,
                     moves the cloud to fit the reference cloud. CloudCompare writes the
                     ICP matrix to a file <name>_SOR_REGISTRATION_MATRIX_<time>.txt in the
                     folder of the input cloud. It is NOT in the log file of the run.
  Stage 4 (Segment)  Same frame as Stage 3.

This script finds these matrices, multiplies them, and writes the poses in the frame of
the cloud that you name. A later stage that needs the position of the sensor can use the
file (or the functions below) without any knowledge of the earlier stages.

All matrices have the form  new = M @ old  (column vectors). A pose P (4 x 4, sensor in the
map) in the new frame is  M @ P.

Usage
-----
    # A project (project.json is found above the cloud). Writes <cloud name>.poses.txt
    # next to the cloud, in the frame of that cloud:
    python frame_chain.py --cloud scans/test_x/03_cleanup/hesai2_scan_cleanup_002.ply

    # The odometry path instead of the SLAM path:
    python frame_chain.py --cloud <cloud.ply> --kind odometry

    # Only show the chain of matrices (writes nothing):
    python frame_chain.py --cloud <cloud.ply> --show

    # Without a project: name the files yourself
    python frame_chain.py --poses map.poses.txt --level-transform level_001_transform.json \\
        --icp-matrix level_001_SOR_REGISTRATION_MATRIX_2026-10-20_17h04_18_916.txt \\
        --output cleanup.poses.txt

How the matrices are found (project mode)
-----------------------------------------
1. project.json gives, for the scan (or the baseline), the output of the stages slam, level,
   cleanup and segment.
2. Level: the file <level cloud name>_transform.json. If it is not there (a Level run
   before Update 30), the script estimates the matrix from the two clouds (Stage 1 map
   and leveled cloud). This needs the same number of points in both clouds in the same
   order, which is true unless Level ran with --remove-below-floor. The script prints the
   size of the fit error.
3. Cleanup: the log file of the run (log_path in project.json) gives the command of
   CloudCompare. If the command has no -ICP, there is no ICP matrix (the matrix is the
   identity). Else the script looks for <leveled cloud name>_SOR_REGISTRATION_MATRIX_*.txt
   in the folder of the leveled cloud and takes the file whose time is nearest to the time
   of the log ("Generated:"), not more than 120 seconds away.

What this script cannot do
--------------------------
- The poses file must be from the same run as the cloud. If you run Stage 1 again, the map
  and the poses file are replaced together, but a Level or Cleanup output of the earlier
  run does not fit them.
- The script finds the ICP matrix of the LAST Cleanup run that made the cloud (by the time
  in the log). It cannot know if you moved the cloud in another program.
- It checks the matrices only in the ways that the messages show. It cannot show that the
  poses are correct, only that they are moved by the same matrices as the cloud.
"""
import argparse
import datetime as _dt
import json
import math
import re
import sys
from pathlib import Path

import numpy as np

STAGES = ("slam", "level", "cleanup", "segment")
MATRIX_NAME_TIME = re.compile(r"_REGISTRATION_MATRIX_(\d{4})-(\d{2})-(\d{2})_(\d{2})h(\d{2})_(\d{2})_(\d{3})\.txt$")
MAX_LOG_DISTANCE_S = 120.0


class ChainError(Exception):
    """A problem that the user can fix. The text says what to do."""


# ---------------------------------------------------------------------------
# Small file readers and writers (no other module of the project is needed)
# ---------------------------------------------------------------------------

def matrix_to_quaternion(rotation):
    """Rotation matrix -> (qx, qy, qz, qw)."""
    m = np.asarray(rotation, dtype=np.float64)
    trace = m[0, 0] + m[1, 1] + m[2, 2]
    if trace > 0:
        s = math.sqrt(trace + 1.0) * 2
        w = 0.25 * s
        x, y, z = (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        w, x = (m[2, 1] - m[1, 2]) / s, 0.25 * s
        y, z = (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        w, y = (m[0, 2] - m[2, 0]) / s, 0.25 * s
        x, z = (m[0, 1] + m[1, 0]) / s, (m[1, 2] + m[2, 1]) / s
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        w, z = (m[1, 0] - m[0, 1]) / s, 0.25 * s
        x, y = (m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s
    q = np.array([x, y, z, w])
    return tuple(q / np.linalg.norm(q))


def quaternion_to_matrix(qx, qy, qz, qw):
    n = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    x, y, z, w = qx / n, qy / n, qz / n, qw / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def read_tum(path):
    """TUM file -> (times N, poses N x 4 x 4)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise ChainError(f"The poses file '{path}' cannot be read ({error}).")
    rows = [line.split() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    if not rows:
        raise ChainError(f"The poses file '{path}' has no lines.")
    try:
        values = np.array([[float(v) for v in row] for row in rows])
    except ValueError:
        raise ChainError(f"The poses file '{path}' is not in TUM format "
                         f"(time x y z qx qy qz qw on each line).")
    if values.ndim != 2 or values.shape[1] != 8:
        raise ChainError(f"The poses file '{path}' must be in TUM format "
                         f"(8 values on each line).")
    poses = np.tile(np.eye(4), (len(values), 1, 1))
    for i, row in enumerate(values):
        poses[i, :3, :3] = quaternion_to_matrix(*row[4:8])
        poses[i, :3, 3] = row[1:4]
    return values[:, 0], poses


def write_tum(path, times, poses):
    """Writes the poses in TUM format, the same layout as slam_kiss_icp.py."""
    lines = []
    for t, pose in zip(times, poses):
        qx, qy, qz, qw = matrix_to_quaternion(pose[:3, :3])
        lines.append(f"{t:.9f} {pose[0, 3]:.9f} {pose[1, 3]:.9f} {pose[2, 3]:.9f} "
                     f"{qx:.9f} {qy:.9f} {qz:.9f} {qw:.9f}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def read_matrix_txt(path):
    """A 4 x 4 matrix in a plain text file (the CloudCompare registration matrix)."""
    try:
        M = np.loadtxt(str(path), dtype=np.float64)
    except (OSError, ValueError) as error:
        raise ChainError(f"The matrix file '{path}' cannot be read ({error}).")
    if M.shape != (4, 4):
        raise ChainError(f"The matrix file '{path}' must have 4 lines of 4 numbers "
                         f"(it has shape {M.shape}).")
    return check_rigid(M, f"the matrix in '{path}'")


def check_rigid(M, what, tolerance=1e-6):
    """Raises ChainError unless M is a 4 x 4 rigid transform (rotation + shift)."""
    M = np.asarray(M, dtype=np.float64)
    if M.shape != (4, 4) or not np.isfinite(M).all():
        raise ChainError(f"{what} is not a finite 4 x 4 matrix.")
    R = M[:3, :3]
    if (np.abs(R @ R.T - np.eye(3)).max() > tolerance or abs(np.linalg.det(R) - 1) > tolerance
            or np.abs(M[3] - [0, 0, 0, 1]).max() > tolerance):
        raise ChainError(f"{what} is not a rigid transform (a rotation and a shift). "
                         f"A scale or a mirror in the matrix is not supported.")
    return M


def read_level_transform(path):
    """The file that level_cloud.py writes -> (4 x 4 matrix, record dict)."""
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ChainError(f"The transform file '{path}' cannot be read ({error}).")
    if not isinstance(record, dict) or record.get("kind") != "level_transform" \
            or "matrix" not in record:
        raise ChainError(f"'{path}' is not a transform file of level_cloud.py.")
    try:
        M = np.array(record["matrix"], dtype=np.float64)
    except (TypeError, ValueError):
        raise ChainError(f"The matrix in '{path}' is not a table of numbers.")
    return check_rigid(M, f"the matrix in '{path}'"), record


def ply_header(path):
    """-> (number of vertices, [(property type, name), ...], byte offset of the data, format)."""
    n, props, fmt = None, [], None
    try:
        f = open(path, "rb")
    except OSError as error:
        raise ChainError(f"The file '{path}' cannot be read ({error}).")
    with f:
        first = f.readline().strip()
        if first != b"ply":
            raise ChainError(f"'{path}' is not a PLY file.")
        in_vertex = False
        while True:
            line = f.readline()
            if not line:
                raise ChainError(f"The header of '{path}' does not end.")
            parts = line.decode("ascii", "replace").split()
            if not parts:
                continue
            if parts[0] == "format":
                fmt = parts[1]
            elif parts[0] == "element":
                in_vertex = parts[1] == "vertex"
                if in_vertex:
                    n = int(parts[2])
            elif parts[0] == "property" and in_vertex:
                props.append((parts[1], parts[-1]))
            elif parts[0] == "end_header":
                return n, props, f.tell(), fmt


_PLY_TYPES = {"char": "i1", "uchar": "u1", "short": "<i2", "ushort": "<u2", "int": "<i4",
              "uint": "<u4", "float": "<f4", "double": "<f8", "int8": "i1", "uint8": "u1",
              "int16": "<i2", "uint16": "<u2", "int32": "<i4", "uint32": "<u4",
              "float32": "<f4", "float64": "<f8"}


def read_ply_xyz(path):
    """The x, y, z columns of a PLY file as an N x 3 float64 array (binary little endian
    or ASCII). Other vertex properties are skipped."""
    n, props, offset, fmt = ply_header(path)
    if n is None or not {"x", "y", "z"} <= {name for _t, name in props}:
        raise ChainError(f"'{path}' has no x, y, z vertex properties.")
    if fmt == "binary_little_endian":
        try:
            dt = np.dtype([(name, _PLY_TYPES[t]) for t, name in props])
        except KeyError as error:
            raise ChainError(f"'{path}': the property type {error} is not supported.")
        with open(path, "rb") as f:
            f.seek(offset)
            data = np.frombuffer(f.read(n * dt.itemsize), dtype=dt)
        return np.stack([data["x"], data["y"], data["z"]], axis=1).astype(np.float64)
    if fmt == "ascii":
        names = [name for _t, name in props]
        cols = [names.index(c) for c in "xyz"]
        with open(path, "rb") as f:
            f.seek(offset)
            arr = np.loadtxt(f, dtype=np.float64, max_rows=n, usecols=cols)
        return arr.reshape(-1, 3)
    raise ChainError(f"'{path}': the PLY format '{fmt}' is not supported "
                     f"(binary little endian or ASCII only).")


def estimate_rigid(source, target):
    """Best rigid transform M with target ~ M @ source (Kabsch). Returns (M, max error, rms)."""
    source = np.asarray(source, dtype=np.float64)
    target = np.asarray(target, dtype=np.float64)
    cs, ct = source.mean(axis=0), target.mean(axis=0)
    U, _S, Vt = np.linalg.svd((source - cs).T @ (target - ct))
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    R = Vt.T @ np.diag([1.0, 1.0, d]) @ U.T
    t = ct - R @ cs
    M = np.eye(4)
    M[:3, :3], M[:3, 3] = R, t
    err = np.linalg.norm(source @ R.T + t - target, axis=1)
    return M, float(err.max()), float(np.sqrt((err ** 2).mean()))


# ---------------------------------------------------------------------------
# The steps of the chain
# ---------------------------------------------------------------------------

class Step:
    def __init__(self, stage, matrix, source, notes=()):
        self.stage = stage          # "slam", "level", "cleanup"
        self.matrix = matrix        # 4 x 4, from the frame before this stage
        self.source = source        # text: where the matrix is from
        self.notes = list(notes)


class Chain:
    """The steps from the Stage 1 map to a later frame."""

    def __init__(self, entity, target):
        self.entity = entity
        self.target = target
        self.steps = []
        self.warnings = []

    @property
    def matrix(self):
        M = np.eye(4)
        for step in self.steps:
            M = step.matrix @ M
        return M

    def describe(self):
        lines = [f"Chain for {self.entity} to the frame of stage '{self.target}':"]
        if not self.steps:
            lines.append("  (no step: the frame of Stage 1)")
        for step in self.steps:
            M = step.matrix
            R = M[:3, :3]
            angle = math.degrees(math.acos(max(-1.0, min(1.0, (np.trace(R) - 1) / 2))))
            lines.append(f"  {step.stage}: rotation {angle:.3f} deg, shift "
                         f"({M[0, 3]:.4f}, {M[1, 3]:.4f}, {M[2, 3]:.4f}) m. {step.source}")
            lines += [f"      {note}" for note in step.notes]
        total = self.matrix
        lines.append("  Total matrix (new = M @ old):")
        lines += ["    " + " ".join(f"{v: .9f}" for v in row) for row in total]
        lines += [f"  WARNING: {w}" for w in self.warnings]
        return "\n".join(lines)


def level_sidecar_path(level_ply):
    p = Path(level_ply)
    return p.with_name(p.stem + "_transform.json")


def level_step(level_ply, slam_ply=None, estimate_max_points=300000):
    """The Level step for the leveled cloud level_ply. Uses the transform file. If the file
    is missing, estimates the matrix from slam_ply (needs the same points in the same order)."""
    level_ply = Path(level_ply)
    sidecar = level_sidecar_path(level_ply)
    warnings = []
    if sidecar.is_file():
        M, record = read_level_transform(sidecar)
        notes = []
        if level_ply.is_file():
            n_cloud = ply_header(level_ply)[0]
            if record.get("n_points_out") not in (None, n_cloud):
                warnings.append(f"The transform file says {record.get('n_points_out')} points, "
                                f"but '{level_ply.name}' has {n_cloud}. The file may be from "
                                f"another Level run. Run Level again.")
            elif sidecar.stat().st_mtime < level_ply.stat().st_mtime - 60:
                warnings.append(f"'{sidecar.name}' is more than 60 seconds older than "
                                f"'{level_ply.name}'. The cloud may be from a Level run with "
                                f"--no-transform-file. Check, or run Level again.")
        return Step("level", M, f"From the file {sidecar.name}.", notes), warnings
    if slam_ply is None or not Path(slam_ply).is_file() or not level_ply.is_file():
        raise ChainError(
            f"There is no file '{sidecar.name}' next to the leveled cloud, and the clouds for an "
            f"estimate are not both available. Run Stage 2 (Level) again with update 30, or give "
            f"--level-transform.")
    n_a, n_b = ply_header(slam_ply)[0], ply_header(level_ply)[0]
    if n_a != n_b:
        raise ChainError(
            f"There is no file '{sidecar.name}', and the Stage 1 map has {n_a} points but the "
            f"leveled cloud has {n_b} (Level ran with --remove-below-floor?). The matrix cannot "
            f"be estimated. Run Stage 2 (Level) again with update 30.")
    a, b = read_ply_xyz(slam_ply), read_ply_xyz(level_ply)
    step = max(1, len(a) // estimate_max_points)
    M, err_max, err_rms = estimate_rigid(a[::step], b[::step])
    if err_max > 1e-3:
        raise ChainError(
            f"There is no file '{sidecar.name}'. The estimate from the clouds does not fit "
            f"(largest error {err_max:.4f} m): the points are not in the same order, or the "
            f"leveled cloud is not from this map. Run Stage 2 (Level) again with update 30.")
    check_rigid(M, "the estimated Level matrix")
    return (Step("level", M, "Estimated from the Stage 1 map and the leveled cloud "
                 "(no transform file).",
                 [f"Fit error of the estimate: largest {err_max:.2e} m, rms {err_rms:.2e} m, "
                  f"{len(a[::step])} points."]), warnings)


# --- CloudCompare log and matrix ------------------------------------------------------------

def parse_cc_log(log_path):
    """Reads the run report of the Cleanup stage. Returns a dict with:
    command, clouds (the -O paths in order), icp (True/False), reference_is_first,
    generated (datetime or None), rms (float or None)."""
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise ChainError(f"The log file '{log_path}' cannot be read ({error}).")
    command = None
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("COMMAND EXECUTED"):
            for follow in lines[i + 1:i + 6]:
                if follow.strip():
                    command = follow.strip()
                    break
            break
    if command is None:
        raise ChainError(f"The log file '{log_path}' has no 'COMMAND EXECUTED' line. It is "
                         f"not a log of the Cleanup stage.")
    clouds = [m.strip() for m in re.findall(r"-O\s+(.+?)(?=\s+-[A-Z_]+(?:\s|$)|\s*$)", command)]
    generated = None
    m = re.search(r"Generated:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", text)
    if m:
        generated = _dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    rms = None
    m = re.search(r"^\S*\s*RMS:\s*([0-9.eE+-]+)", text, re.MULTILINE)
    if m is None:
        m = re.search(r"RMS:\s*([0-9.eE+-]+)", text)
    if m:
        try:
            rms = float(m.group(1))
        except ValueError:
            rms = None
    tokens = re.findall(r"(?:^|\s)(-[A-Z_]+)(?=\s|$)", command)
    return {"command": command, "clouds": clouds, "icp": "-ICP" in tokens,
            "reference_is_first": "-REFERENCE_IS_FIRST" in tokens,
            "generated": generated, "rms": rms}


def _windows_stem(text):
    """The file name without folder and extension, for a Windows or Linux path."""
    name = re.split(r"[\\/]", str(text))[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def matrix_file_time(path):
    m = MATRIX_NAME_TIME.search(Path(path).name)
    if not m:
        return None
    y, mo, d, h, mi, s, ms = (int(v) for v in m.groups())
    return _dt.datetime(y, mo, d, h, mi, s, ms * 1000)


def find_registration_matrix(moved_stem, generated, search_dirs):
    """The CloudCompare matrix file of the cloud that was moved (name `moved_stem`).
    Returns (path, distance in seconds) or raises ChainError."""
    candidates = []
    for folder in search_dirs:
        folder = Path(folder)
        if folder.is_dir():
            candidates += list(folder.glob(f"{moved_stem}_SOR_REGISTRATION_MATRIX_*.txt"))
            candidates += list(folder.glob(f"{moved_stem}_REGISTRATION_MATRIX_*.txt"))
    candidates = sorted(set(candidates))
    if not candidates:
        raise ChainError(
            f"No file '{moved_stem}_SOR_REGISTRATION_MATRIX_*.txt' in "
            f"{', '.join(str(d) for d in search_dirs)}. CloudCompare writes it to the folder of "
            f"the input cloud when the ICP runs. Find it, and give it with --icp-matrix.")
    timed = [(c, matrix_file_time(c)) for c in candidates]
    timed = [(c, t) for c, t in timed if t is not None]
    if not timed:
        return candidates[-1], None
    if generated is None:
        c, t = max(timed, key=lambda item: item[1])
        return c, None
    best, best_d = None, None
    for c, t in timed:
        d = abs((t - generated).total_seconds())
        if best_d is None or d < best_d:
            best, best_d = c, d
    if best_d > MAX_LOG_DISTANCE_S:
        raise ChainError(
            f"The registration matrices of '{moved_stem}' are {best_d:.0f} s or more away from "
            f"the time of the log ({generated}). The nearest is '{best.name}'. This is probably "
            f"not the matrix of this run. Give the right file with --icp-matrix.")
    return best, best_d


def cleanup_step(log_path, level_ply, extra_search_dirs=(), rms_expected=None):
    """The Cleanup step from the log of the run. Returns (Step, warnings)."""
    info = parse_cc_log(log_path)
    warnings = []
    if len(info["clouds"]) < 2:
        raise ChainError(f"The command in '{log_path}' has fewer than 2 clouds (-O). The "
                         f"script does not know which cloud was moved.")
    if not info["icp"]:
        return (Step("cleanup", np.eye(4), "No ICP in the CloudCompare command: the cloud is "
                     "not moved (SOR only removes points).", []), warnings)
    if not info["reference_is_first"]:
        raise ChainError(
            "The CloudCompare command has -ICP without -REFERENCE_IS_FIRST. Then CloudCompare "
            "moves the FIRST cloud. The script supports only the case where the second "
            "(input) cloud is moved. Give the matrix with --icp-matrix.")
    moved = info["clouds"][1]
    moved_stem = _windows_stem(moved)
    level_stem = Path(level_ply).stem if level_ply else None
    if level_stem and moved_stem != level_stem:
        warnings.append(f"The log says that the moved cloud is '{moved_stem}', but the leveled "
                        f"cloud of the project is '{level_stem}'. The Cleanup output may be from "
                        f"another Level output.")
    dirs = [Path(level_ply).parent] if level_ply else []
    dirs += [Path(d) for d in extra_search_dirs]
    path, distance = find_registration_matrix(moved_stem, info["generated"], dirs)
    M = read_matrix_txt(path)
    notes = []
    if distance is not None:
        notes.append(f"The time of the matrix file is {distance:.1f} s from the time of the log.")
    if info["rms"] is not None:
        notes.append(f"RMS of the ICP in the log: {info['rms']:g} m.")
        if rms_expected is not None and abs(rms_expected - info["rms"]) > 1e-6:
            warnings.append(f"The RMS in the log ({info['rms']:g}) is not the RMS in project.json "
                            f"({rms_expected:g}).")
    return Step("cleanup", M, f"From the CloudCompare file {path.name}.", notes), warnings


# ---------------------------------------------------------------------------
# Project mode
# ---------------------------------------------------------------------------

def find_project_json(start):
    p = Path(start).resolve()
    for folder in [p] + list(p.parents):
        candidate = folder / "project.json"
        if candidate.is_file():
            return candidate
    return None


def locate(project_dir, text):
    """Path of a file named in project.json. A relative path is relative to the project folder.
    A full path of another computer is tried by its end."""
    if not text:
        return None
    text = str(text)
    parts = [x for x in re.split(r"[\\/]", text) if x]
    direct = Path(project_dir) / Path(*parts) if not re.match(r"^([A-Za-z]:|[\\/])", text) else None
    if direct is not None and direct.exists():
        return direct
    absolute = Path(text)
    if absolute.exists():
        return absolute
    for k in range(1, len(parts)):                    # drop leading parts one by one
        candidate = Path(project_dir) / Path(*parts[k:])
        if candidate.exists():
            return candidate
    return direct or Path(project_dir) / Path(*parts[-1:])


def load_project(project_json):
    try:
        return json.loads(Path(project_json).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ChainError(f"project.json '{project_json}' cannot be read ({error}).")


def entities(project):
    """-> {name: stages dict}. 'baseline' and each scan."""
    result = {}
    if isinstance(project.get("baseline"), dict):
        result["baseline"] = project["baseline"].get("stages", {})
    for name, scan in (project.get("scans") or {}).items():
        result[name] = scan.get("stages", {})
    return result


def identify_cloud(project, project_dir, cloud):
    """Finds the entity and the stage whose output is `cloud`. -> (entity, stage)."""
    cloud = Path(cloud).resolve()
    found = []
    for name, stages in entities(project).items():
        for stage in STAGES:
            out = (stages.get(stage) or {}).get("output")
            if out and Path(locate(project_dir, out)).resolve() == cloud:
                found.append((name, stage))
    if not found:
        raise ChainError(f"'{cloud}' is not the output of a stage in project.json. Use the "
                         f"manual options (--poses, --level-transform, --icp-matrix), or name "
                         f"a cloud that a stage made.")
    return found[0]


def chain_for(project, project_dir, entity, target):
    """The Chain from the Stage 1 map of `entity` to the frame of stage `target`."""
    if target not in STAGES:
        raise ChainError(f"The stage must be one of {', '.join(STAGES)}.")
    stages = entities(project).get(entity)
    if stages is None:
        raise ChainError(f"'{entity}' is not in project.json.")
    chain = Chain(entity, target)
    if target == "slam":
        return chain
    level_rec = stages.get("level") or {}
    slam_rec = stages.get("slam") or {}
    if level_rec.get("status") != "complete" or not level_rec.get("output"):
        raise ChainError(f"The stage 'level' of '{entity}' is not complete in project.json.")
    level_ply = locate(project_dir, level_rec["output"])
    slam_ply = locate(project_dir, slam_rec.get("output")) if slam_rec.get("output") else None
    step, warnings = level_step(level_ply, slam_ply)
    chain.steps.append(step)
    chain.warnings += warnings
    if target == "level":
        return chain
    cleanup_rec = stages.get("cleanup") or {}
    if cleanup_rec.get("status") != "complete":
        raise ChainError(f"The stage 'cleanup' of '{entity}' is not complete in project.json.")
    log = cleanup_rec.get("log_path")
    if not log:
        raise ChainError(f"The record of the stage 'cleanup' of '{entity}' has no log_path. "
                         f"Use --icp-matrix to give the matrix.")
    step, warnings = cleanup_step(locate(project_dir, log), level_ply,
                                  rms_expected=cleanup_rec.get("icp_rms"))
    chain.steps.append(step)
    chain.warnings += warnings
    return chain


def poses_file_for(project, project_dir, entity, kind="poses"):
    """The file with the Stage 1 poses ('poses') or the odometry path ('odometry') of an entity."""
    stages = entities(project)[entity]
    slam = stages.get("slam") or {}
    if not slam.get("output"):
        raise ChainError(f"The stage 'slam' of '{entity}' has no output.")
    slam_ply = locate(project_dir, slam["output"])
    key = "poses_file" if kind == "poses" else "odometry_file"
    if slam.get(key):
        recorded = locate(project_dir, slam[key])
        if recorded.is_file():
            return recorded
    suffix = ".poses.txt" if kind == "poses" else ".odometry.txt"
    conventional = Path(slam_ply).with_suffix(suffix)
    if conventional.is_file():
        return conventional
    option = "--save-poses (Save the poses)" if kind == "poses" else "--save-odometry"
    raise ChainError(f"There is no {kind} file for '{entity}': '{conventional.name}' is not next "
                     f"to the Stage 1 map. Run Stage 1 again with {option}.")


def poses_in_frame_of(cloud, kind="poses", project_json=None):
    """For a later stage: the poses (or the odometry path) of the sensor in the frame of
    `cloud` (the output of a stage in a project).
    Returns (times, poses N x 4 x 4, Chain, source file)."""
    project_json = Path(project_json) if project_json else find_project_json(cloud)
    if project_json is None:
        raise ChainError(f"No project.json above '{cloud}'.")
    project = load_project(project_json)
    project_dir = project_json.parent
    entity, stage = identify_cloud(project, project_dir, cloud)
    source = poses_file_for(project, project_dir, entity, kind)
    chain = chain_for(project, project_dir, entity, stage)
    times, poses = read_tum(source)
    return times, apply_to_poses(chain.matrix, poses), chain, source


def apply_to_poses(M, poses):
    """M @ P for every pose P."""
    M = np.asarray(M, dtype=np.float64)
    return np.einsum("ij,njk->nik", M, np.asarray(poses, dtype=np.float64))


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Move the Stage 1 poses (or the odometry path) into the frame of a later "
                    "stage. See the top of this file.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cloud", help="a cloud that a stage made (project mode)")
    ap.add_argument("--project", help="project.json (default: the first one above the cloud)")
    ap.add_argument("--kind", choices=("poses", "odometry"), default="poses",
                    help="poses = the SLAM path (--save-poses); odometry = the QBot path "
                         "(--save-odometry). Default: poses")
    ap.add_argument("--output", help="the new poses file (default: <cloud name>.<kind>.txt "
                                     "next to the cloud)")
    ap.add_argument("--show", action="store_true", help="print the chain, write nothing")
    ap.add_argument("--poses", help="manual mode: a TUM poses file in the Stage 1 frame")
    ap.add_argument("--level-transform", help="manual mode: the JSON file of level_cloud.py")
    ap.add_argument("--icp-matrix", help="manual mode: the CloudCompare registration matrix file")
    args = ap.parse_args(argv)

    try:
        if args.cloud:
            if args.poses or args.level_transform or args.icp_matrix:
                ap.error("Use --cloud (project mode) or --poses (manual mode), not both.")
            project_json = Path(args.project) if args.project else find_project_json(args.cloud)
            if project_json is None:
                raise ChainError(f"No project.json above '{args.cloud}'. Use --project, or the "
                                 f"manual options.")
            project = load_project(project_json)
            project_dir = project_json.parent
            entity, stage = identify_cloud(project, project_dir, args.cloud)
            chain = chain_for(project, project_dir, entity, stage)
            print(f"Project {project_json}: {entity}, stage '{stage}'.")
            print(chain.describe())
            if args.show:
                return 0
            source = poses_file_for(project, project_dir, entity, args.kind)
            times, poses = read_tum(source)
            out = Path(args.output) if args.output else \
                Path(args.cloud).with_suffix(f".{args.kind}.txt")
        else:
            if not args.poses:
                ap.error("Name a cloud (--cloud) or a poses file (--poses).")
            if not args.output and not args.show:
                ap.error("Manual mode needs --output.")
            chain = Chain("manual", "manual")
            if args.level_transform:
                M, _record = read_level_transform(args.level_transform)
                chain.steps.append(Step("level", M, f"From the file {Path(args.level_transform).name}."))
            if args.icp_matrix:
                chain.steps.append(Step("cleanup", read_matrix_txt(args.icp_matrix),
                                        f"From the file {Path(args.icp_matrix).name}."))
            if not chain.steps:
                ap.error("Manual mode needs --level-transform and/or --icp-matrix.")
            print(chain.describe())
            if args.show:
                return 0
            source = Path(args.poses)
            times, poses = read_tum(source)
            out = Path(args.output)
        if not chain.steps and out.resolve() == Path(source).resolve():
            print(f"The cloud is in the frame of Stage 1. The poses file {source} is already in "
                  f"this frame. Nothing written.")
            return 0
        moved = apply_to_poses(chain.matrix, poses)
        n = write_tum(out, times, moved)
        print(f"Read {len(times)} poses from {source}")
        print(f"Wrote {n} poses to {out}")
        return 0
    except ChainError as error:
        print(f"ERROR: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
