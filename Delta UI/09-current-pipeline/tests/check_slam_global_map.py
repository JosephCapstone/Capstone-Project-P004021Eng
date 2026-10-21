#!/usr/bin/env python3
"""
test_slam_global_map.py
=======================
Checks that slam_kiss_icp.py saves the map of ALL frames by default (not only
the kiss-icp local map near the final robot position), and that the options
--local-map and --map-max-range work. Uses a simulated room (12 x 8 x 2.5 m)
and a loop path that ends at the start. Skips (exit code 0) if kiss-icp is not
installed.

Run:  python test_slam_global_map.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

try:
    import kiss_icp  # noqa: F401
except ImportError:
    print("SKIPPED: kiss-icp is not installed.")
    sys.exit(0)

from plyfile import PlyData

SCRIPT = Path(__file__).resolve().parents[1] / "pipeline/Scripts" / "slam_kiss_icp.py"
failures = []


def check(name, ok, detail=""):
    print(
        ("PASS  " if ok else "FAIL  ")
        + name
        + (f"   {detail}" if detail and not ok else "")
    )
    if not ok:
        failures.append(name)


def make_frames(folder):
    rng = np.random.default_rng(3)
    lo = np.array([-6, -4, 0.0])
    hi = np.array([6, 4, 2.5])
    boxes = [
        (np.array([-1, -1, 0]), np.array([0.5, 0.5, 1.2])),
        (np.array([2, 1.5, 0]), np.array([3.2, 2.7, 2.0])),
        (np.array([-4, 1, 0]), np.array([-3.4, 1.6, 2.5])),
    ]
    az = np.radians(np.arange(0, 360, 1.0))
    el = np.radians(np.linspace(-15, 15, 10))
    a_grid, e_grid = np.meshgrid(az, el)
    d = np.c_[
        (np.cos(e_grid) * np.cos(a_grid)).ravel(),
        (np.cos(e_grid) * np.sin(a_grid)).ravel(),
        np.sin(e_grid).ravel(),
    ]

    def cast(o):
        with np.errstate(divide="ignore", invalid="ignore"):
            t_far = np.where(d > 0, (hi - o) / d, (lo - o) / d)
        t_far[~np.isfinite(t_far)] = np.inf
        t = t_far.min(1)
        for bl, bh in boxes:
            with np.errstate(divide="ignore", invalid="ignore"):
                a = (bl - o) / d
                b = (bh - o) / d
            tn = np.where(np.isnan(np.minimum(a, b)), -np.inf, np.minimum(a, b))
            tf = np.where(np.isnan(np.maximum(a, b)), np.inf, np.maximum(a, b))
            tmin, tmax = tn.max(1), tf.min(1)
            hit = (tmax >= np.maximum(tmin, 0)) & (tmin > 0)
            t = np.where(hit & (tmin < t), tmin, t)
        return t

    waypoints = np.array([[-5, -3], [5, -3], [5, 3], [-5, 3], [-5, -3]], float)
    poses, yaw = [], 0.0
    for a, b in zip(waypoints[:-1], waypoints[1:]):
        heading = np.arctan2(*(b - a)[::-1])
        turn = (heading - yaw + np.pi) % (2 * np.pi) - np.pi
        n_turn = max(1, int(abs(turn) / np.radians(3)))
        for k in range(1, n_turn + 1):
            poses.append((a[0], a[1], yaw + turn * k / n_turn))
        yaw = heading
        n_step = int(np.linalg.norm(b - a) / 0.2)
        for k in range(1, n_step + 1):
            poses.append(
                (
                    a[0] + (b[0] - a[0]) * k / n_step,
                    a[1] + (b[1] - a[1]) * k / n_step,
                    yaw,
                )
            )
    for i, (x, y, yw) in enumerate(poses):
        t = cast(np.array([x, y, 1.0]))
        ok = np.isfinite(t) & (t > 0.3) & (t < 30)
        p = d[ok] * t[ok, None] + rng.normal(0, 0.01, (int(ok.sum()), 3))
        c, s = np.cos(-yw), np.sin(-yw)
        rot = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
        ps = p @ rot.T
        np.c_[ps, np.zeros(len(ps))].astype(np.float32).tofile(folder / f"{i:05d}.bin")
    return len(poses)


def run(frames, out, *extra):
    cmd = [
        sys.executable,
        str(SCRIPT),
        "--input",
        str(frames),
        "--dataloader",
        "generic",
        "--max-range",
        "10",
        "--voxel-size",
        "0.15",
        "--output",
        str(out),
        *extra,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def radius_from_start(ply):
    v = PlyData.read(str(ply))["vertex"]
    p = np.c_[v["x"], v["y"], v["z"]]
    return len(p), float(np.linalg.norm(p, axis=1).max())


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    frames = tmp / "frames"
    frames.mkdir()
    n = make_frames(frames)
    print(f"{n} simulated frames")

    r_default = run(frames, tmp / "default.ply")
    r_local = run(frames, tmp / "local.ply", "--local-map")
    r_limit = run(frames, tmp / "limit.ply", "--map-max-range", "5")
    r_conflict = run(
        frames, tmp / "conflict.ply", "--local-map", "--map-max-range", "5"
    )

    check(
        "default run succeeds",
        r_default.returncode == 0,
        r_default.stdout[-300:] + r_default.stderr[-300:],
    )
    check("--local-map run succeeds", r_local.returncode == 0)
    check("--map-max-range run succeeds", r_limit.returncode == 0)
    check(
        "default prints that it saves the global map",
        "global map of all frames" in r_default.stdout,
    )
    check(
        "--local-map prints that it saves the local map", "local map" in r_local.stdout
    )
    check(
        "--local-map with --map-max-range is refused",
        r_conflict.returncode == 1 and "cannot be used" in r_conflict.stdout,
    )
    check("refused run writes no file", not (tmp / "conflict.ply").exists())

    n_default, rad_default = radius_from_start(tmp / "default.ply")
    n_local, rad_local = radius_from_start(tmp / "local.ply")
    n_limit, rad_limit = radius_from_start(tmp / "limit.ply")
    print(
        f"default {n_default} pts radius {rad_default:.2f}; local {n_local} pts radius "
        f"{rad_local:.2f}; limit {n_limit} pts radius {rad_limit:.2f}"
    )
    # The local map is a ball of data.max_range (10 m) around the final position = the start.
    check(
        "local map stays inside max_range of the start",
        rad_local <= 10.3,
        f"{rad_local:.2f}",
    )
    check(
        "default map reaches farther than the local map (far end of the room)",
        rad_default > rad_local + 1.0,
        f"{rad_default:.2f} vs {rad_local:.2f}",
    )
    check("default map has more points than the local map", n_default > n_local)
    check("--map-max-range removes points from the default map", n_limit < n_default)

    # Changed 2026-10-15: --map-max-range alone also replaces the registration range.
    check(
        "--max-range 10 with --map-max-range 5 keeps registration at 10 m",
        "Registration max range set to 10.0 m by --max-range" in r_limit.stdout,
        r_limit.stdout[-400:],
    )

    def run_no_max_range(out, *extra):
        cmd = [
            sys.executable,
            str(SCRIPT),
            "--input",
            str(frames),
            "--dataloader",
            "generic",
            "--voxel-size",
            "0.15",
            "--output",
            str(out),
            *extra,
        ]
        return subprocess.run(cmd, capture_output=True, text=True)

    r_mmr = run_no_max_range(tmp / "mmr.ply", "--map-max-range", "5")
    r_cfg = run_no_max_range(tmp / "cfg.ply")
    check(
        "--map-max-range alone succeeds",
        r_mmr.returncode == 0,
        r_mmr.stdout[-300:] + r_mmr.stderr[-300:],
    )
    check(
        "--map-max-range alone sets the registration range to 5 m",
        "Registration max range set to 5.0 m by --map-max-range" in r_mmr.stdout,
        r_mmr.stdout[-400:],
    )
    check(
        "no range option keeps the config's range",
        "(from the config)" in r_cfg.stdout
        and "overrides the config" not in r_cfg.stdout,
    )
    # Registration really used 5 m: the map differs from the run that only cropped the saved map.
    n_mmr, rad_mmr = radius_from_start(tmp / "mmr.ply")
    print(f"map-max-range alone {n_mmr} pts radius {rad_mmr:.2f}")
    check(
        "map from --map-max-range alone differs from the crop-only run",
        (n_mmr, round(rad_mmr, 2)) != (n_limit, round(rad_limit, 2)),
    )

print()
print(
    "All checks passed."
    if not failures
    else f"{len(failures)} check(s) FAILED: {failures}"
)
sys.exit(1 if failures else 0)
