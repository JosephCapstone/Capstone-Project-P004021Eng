#!/usr/bin/env python3
"""
level_cloud.py
================
Corrects the skew/tilt in a raw SLAM map. ouster-cli's SLAM backend has no
gravity/leveling step - the whole map's orientation is locked to wherever
the sensor happened to be pointing at the very first frame, so any tilt in
how the QBot was sitting at recording start propagates through the entire
map. This finds the floor via RANSAC, then rotates the whole cloud so
that floor is level and lies at Z=0.

Picking the floor is a two-step process, not just "biggest plane":
1. Find near-horizontal candidates (|normal.z| >= --horizontal-threshold)
   above the minimum size (--min-inlier-fraction) - this already rules out
   walls, which are near-vertical.
2. Among those, pick the LOWEST one by Z centroid - the floor sits at the
   bottom of the room, the ceiling at the top, and a real ceiling can
   easily have MORE points than the floor (a cleaner, less cluttered
   surface, or simply larger if walls slope inward) - confirmed on real
   data as a roof getting picked as "the floor" when candidates were only
   scored by point count. Picking the lowest horizontal candidate instead
   of the biggest one fixes that, and is the same approach
   segment_planes.py already uses (and has validated on real data) to
   split floor vs. ceiling once both are already known to be horizontal.

If NO candidate clears the horizontal threshold at all (a genuinely
unusual scan), this falls back to the old count-weighted-by-horizontality
score, with a clear warning that the fallback path was used - better to
produce a best-effort answer with a visible warning than to fail outright.

Optional (Update 26): --remove-below-floor deletes the stray points that lie
far below the floor (for example ghost returns under the deck). It is off by
default. The script always counts these points in its report, also when the
option is off. How it decides:
1. After levelling, the floor is at Z=0. A plane is only an approximation: a
   floor that is tilted a little or bent by SLAM drift can be several
   centimetres off the plane at one end of the room. So the script finds a
   LOCAL floor: for each cell on the floor (--floor-cell, default 0.5 m) it takes
   the lowest dense layer of points near the plane.
2. A point is "below the floor" when it is lower than the local floor of its
   cell by more than --below-floor-margin (default 0.30 m).
3. Points that are below the floor are joined into groups (points closer than
   --below-floor-link, default 0.10 m, belong to the same group). A group is
   REMOVED when its footprint is smaller than --below-floor-keep-area (default
   0.25 square metres). A larger group is KEPT: it can be real, for example a
   hole in the deck through which the space below is visible.
A mirror image of the room below the floor (a reflection) is a large group
and is also kept. The report lists every kept group.

Meant to run right after Stage 1 (SLAM) and before Stage 3 (Cleanup) - as
Stage 2 itself - ICP alignment in Stage 3 works better on a level cloud,
and every step after that inherits a sane coordinate frame for free.

Requires:
    pip install open3d numpy

Usage:
    python level_cloud.py --input slam_output.ply --output leveled.ply

STATUS: run against real data. The floor-candidate heuristic (lowest
near-horizontal plane above a minimum size, matching segment_planes.py's
own floor/ceiling split) replaced an earlier biggest-plane-wins heuristic
that could pick the ceiling instead of the floor - if it still picks a
wall or the wrong horizontal surface on your actual scans, that will show
up clearly in the console report (a "floor" with very few points, a
rotation that makes things look worse instead of better, or the
low-horizontality warning below) and the thresholds can be adjusted.
"""

import argparse
import sys

import numpy as np
import open3d as o3d


def load_cloud(path):
    pcd = o3d.io.read_point_cloud(str(path))
    if len(pcd.points) == 0:
        raise ValueError(f"'{path}' loaded but has zero points.")
    return pcd


def find_plane_candidates(pcd, max_planes=6, distance_threshold=0.02,
                           ransac_n=3, num_iterations=1000, min_inlier_fraction=0.02):
    """
    Repeatedly finds the largest remaining plane and removes its inliers,
    building a short list of candidate planes (floor, ceiling, walls all
    show up as candidates here - picking which one is the floor happens
    in the caller). Stops early if a found plane is too small to be a
    real structural surface (min_inlier_fraction of the original cloud).
    """
    remaining = pcd
    total_points = len(pcd.points)
    candidates = []

    for _ in range(max_planes):
        if len(remaining.points) < ransac_n * 2:
            break
        plane_model, inliers = remaining.segment_plane(
            distance_threshold, ransac_n, num_iterations)
        if len(inliers) < total_points * min_inlier_fraction:
            break

        a, b, c, d = plane_model
        normal = np.array([a, b, c], dtype=np.float64)
        normal /= np.linalg.norm(normal)
        inlier_points = np.asarray(remaining.select_by_index(inliers).points)

        candidates.append({
            "normal": normal,
            "d": d,
            "count": len(inliers),
            "centroid": inlier_points.mean(axis=0),
        })
        remaining = remaining.select_by_index(inliers, invert=True)

    return candidates


def pick_floor_candidate(candidates, horizontal_threshold=0.7):
    """
    Among candidates that are actually near-horizontal (|normal.z| >=
    horizontal_threshold - this alone already rules out walls), picks the
    LOWEST one by Z centroid as the floor. A real ceiling can have more
    points than a real floor (cleaner surface, less clutter, or simply
    larger geometry), so scoring by point count alone can pick the
    ceiling instead - picking the lowest horizontal candidate avoids that,
    the same way segment_planes.py already splits floor vs. ceiling once
    both are known to be horizontal.

    Falls back to the old count x horizontality score across ALL
    candidates if none clear the horizontal threshold at all (an unusual
    scan) - better to produce a best-effort answer than fail outright,
    but the caller should treat this path as a strong warning sign.

    Returns (floor_candidate_or_None, used_fallback).
    """
    if not candidates:
        return None, False

    horizontal_candidates = [c for c in candidates if abs(c["normal"][2]) >= horizontal_threshold]
    if horizontal_candidates:
        floor = min(horizontal_candidates, key=lambda c: c["centroid"][2])
        return floor, False

    # Nothing cleared the horizontal bar at all - fall back to the old
    # biggest-and-most-horizontal score rather than failing outright.
    floor = max(candidates, key=lambda c: c["count"] * abs(c["normal"][2]))
    return floor, True


def rotation_to_align(source_vec, target_vec):
    """Returns the 3x3 rotation matrix that rotates source_vec onto
    target_vec (both assumed unit vectors already)."""
    v = np.cross(source_vec, target_vec)
    s = np.linalg.norm(v)
    c = np.dot(source_vec, target_vec)

    if s < 1e-8:
        if c > 0:
            return np.eye(3)  # already aligned
        # 180 degrees apart - pick any axis perpendicular to source_vec
        axis = np.array([1.0, 0.0, 0.0])
        if abs(source_vec[0]) > 0.9:
            axis = np.array([0.0, 1.0, 0.0])
        axis = axis - source_vec * np.dot(axis, source_vec)
        axis /= np.linalg.norm(axis)
        vx = np.array([[0, -axis[2], axis[1]],
                        [axis[2], 0, -axis[0]],
                        [-axis[1], axis[0], 0]])
        return np.eye(3) + 2 * (vx @ vx)  # Rodrigues, 180 degree case

    vx = np.array([[0, -v[2], v[1]],
                    [v[2], 0, -v[0]],
                    [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * ((1 - c) / (s ** 2))


def level_point_cloud(pcd, floor):
    """
    Rotates pcd so the floor's normal points along +Z, then translates so
    the floor sits at Z=0. Normal sign is resolved using the rest of the
    cloud's centroid, so "up" ends up pointing away from the floor toward
    the room interior, not arbitrarily either way.
    """
    normal = floor["normal"]
    all_points = np.asarray(pcd.points)
    cloud_centroid = all_points.mean(axis=0)

    # Make sure normal points from the floor toward the rest of the cloud
    to_cloud = cloud_centroid - floor["centroid"]
    if np.dot(normal, to_cloud) < 0:
        normal = -normal

    target = np.array([0.0, 0.0, 1.0])
    R = rotation_to_align(normal, target)

    pcd.rotate(R, center=(0, 0, 0))

    rotated_floor_centroid = R @ floor["centroid"]
    pcd.translate((0, 0, -rotated_floor_centroid[2]))

    return pcd, R


def local_floor_offsets(xy, height, cell=0.5, bin_size=0.01, search=0.20,
                        min_points=30, min_share=0.03, min_count=8):
    """
    Height of the LOCAL floor for every point, relative to the fitted floor
    plane (the cloud is level, so `height` is simply Z).

    The points are put in square cells on the floor (`cell`, metres). In each
    cell the script looks at the points within +-`search` of the plane and
    makes a histogram of their height (`bin_size`, metres). The floor is the
    LOWEST bin that holds enough points (at least `min_count` points and at
    least `min_share` of the points near the plane). The offset of the cell is
    the median height of the points in that bin and the next bin.

    A cell with fewer than `min_points` points near the plane, or with no
    dense bin, has no local floor. Its offset is 0 (the plane).

    Returns (offsets, n_cells_with_floor, n_cells).
    """
    ij = np.floor((xy - xy.min(axis=0)) / cell).astype(np.int64)
    key = ij[:, 0] * 1_000_003 + ij[:, 1]
    offsets = np.zeros(len(height))
    order = np.argsort(key, kind="stable")
    sorted_key = key[order]
    cuts = np.flatnonzero(np.diff(sorted_key)) + 1
    n_known = 0
    n_cells = 0
    for idx in np.split(order, cuts):
        n_cells += 1
        h = height[idx]
        near = h[(h > -search) & (h < search)]
        if len(near) < min_points:
            continue
        bins = np.floor(near / bin_size).astype(np.int64)
        unique_bins, counts = np.unique(bins, return_counts=True)
        dense = np.flatnonzero(counts >= max(min_count, min_share * len(near)))
        if len(dense) == 0:
            continue
        low = unique_bins[dense[0]]
        layer = near[(near >= low * bin_size) & (near < (low + 2) * bin_size)]
        offsets[idx] = np.median(layer)
        n_known += 1
    return offsets, n_known, n_cells


def find_below_floor(points, margin=0.30, cell=0.5, link=0.10, keep_area=0.25,
                     footprint_cell=0.10, report_margins=(0.05, 0.10, 0.30, 0.50)):
    """
    Finds the stray points below the floor. `points` is an (N, 3) array of a
    LEVEL cloud (floor at Z=0).

    Returns a dict:
      depth          depth below the local floor for every point (positive
                     number = below; 0 or negative = on or above the floor)
      remove_mask    True for the points that are in a group smaller than
                     `keep_area` (these are removed with --remove-below-floor)
      groups         list of dicts for all groups of points deeper than
                     `margin`: n_points, area (m2), centre, depth_min,
                     depth_max, removed (True / False)
      counts         {margin: number of points deeper than margin} for
                     `report_margins`
      cells_known, cells_total
    """
    height = points[:, 2]
    offsets, n_known, n_cells = local_floor_offsets(points[:, :2], height, cell=cell)
    depth = offsets - height
    counts = {m: int((depth > m).sum()) for m in report_margins}

    deep = np.flatnonzero(depth > margin)
    remove_mask = np.zeros(len(points), dtype=bool)
    groups = []
    if len(deep) > 0:
        pcd = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points[deep]))
        labels = np.asarray(pcd.cluster_dbscan(eps=link, min_points=1, print_progress=False))
        for g in range(labels.max() + 1):
            members = deep[labels == g]
            p = points[members]
            cells = np.unique(np.floor(p[:, :2] / footprint_cell).astype(np.int64), axis=0)
            area = len(cells) * footprint_cell * footprint_cell
            removed = area < keep_area
            if removed:
                remove_mask[members] = True
            groups.append({
                "n_points": int(len(members)),
                "area": float(area),
                "centre": p.mean(axis=0),
                "depth_min": float(depth[members].min()),
                "depth_max": float(depth[members].max()),
                "removed": bool(removed),
            })
    return {"depth": depth, "remove_mask": remove_mask, "groups": groups,
            "counts": counts, "cells_known": n_known, "cells_total": n_cells}


def print_below_floor_report(result, margin, keep_area, removing):
    counts = result["counts"]
    groups = result["groups"]
    print("\nPoints below the floor (the local floor of each 0.5 m cell; "
          f"local floor found in {result['cells_known']} of {result['cells_total']} cells):")
    for m in sorted(counts):
        print(f"  deeper than {m * 100:>4.0f} cm: {counts[m]} points")
    n_removed_groups = sum(1 for g in groups if g["removed"])
    n_kept_groups = len(groups) - n_removed_groups
    n_removed_points = int(result["remove_mask"].sum())
    n_kept_points = sum(g["n_points"] for g in groups if not g["removed"])
    print(f"  Groups deeper than {margin * 100:.0f} cm: {len(groups)} "
          f"({n_removed_groups} smaller than {keep_area:g} m2 = {n_removed_points} points; "
          f"{n_kept_groups} larger = {n_kept_points} points)")
    for g in sorted([g for g in groups if not g["removed"]], key=lambda g: -g["n_points"])[:10]:
        c = g["centre"]
        print(f"    KEPT group: {g['n_points']} points, footprint {g['area']:.2f} m2, "
              f"centre ({c[0]:.2f}, {c[1]:.2f}, {c[2]:.2f}), "
              f"{g['depth_min'] * 100:.0f} to {g['depth_max'] * 100:.0f} cm below the floor")
    if removing:
        print(f"  REMOVED {n_removed_points} points (groups smaller than {keep_area:g} m2).")
    elif n_removed_points > 0:
        print(f"  Nothing removed (the option --remove-below-floor is off). "
              f"With the option on, {n_removed_points} points would be removed.")
    else:
        print("  Nothing would be removed with the current margin.")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, help="Raw SLAM output .ply")
    parser.add_argument("--output", required=True, help="Leveled output .ply")
    parser.add_argument("--distance-threshold", type=float, default=0.02,
                         help="RANSAC plane-fit tolerance, in the cloud's units "
                              "(usually meters). Default: 0.02")
    parser.add_argument("--max-planes", type=int, default=6,
                         help="How many candidate planes to search through before "
                              "giving up on finding a floor. Default: 6")
    parser.add_argument("--min-inlier-fraction", type=float, default=0.02,
                         help="A candidate plane must contain at least this fraction "
                              "of all points to count (0.02 = 2%%). Lower this if a "
                              "real floor/wall is smaller relative to the whole cloud "
                              "than usual. Default: 0.02")
    parser.add_argument("--horizontal-threshold", type=float, default=0.7,
                         help="|normal.z| above this counts as a floor/ceiling "
                              "candidate rather than a wall - only candidates that "
                              "clear this bar are eligible to be picked as the floor "
                              "at all. Same default as segment_planes.py's own "
                              "horizontal threshold, for consistency. Default: 0.7")
    parser.add_argument("--remove-below-floor", action="store_true",
                         help="Remove the stray points that are far below the floor "
                              "(see the text at the top of this file). Off by default. "
                              "The report counts these points also when this option is off.")
    parser.add_argument("--below-floor-margin", type=float, default=0.30,
                         help="A point is 'below the floor' when it is lower than the "
                              "local floor by more than this distance, in metres. "
                              "Default: 0.30")
    parser.add_argument("--below-floor-keep-area", type=float, default=0.25,
                         help="A group of points below the floor is kept (not removed) "
                              "when its footprint is at least this area, in square "
                              "metres. A large group can be real (for example a hole in "
                              "the deck). 0 keeps every group, so nothing is removed. "
                              "Default: 0.25")
    parser.add_argument("--below-floor-link", type=float, default=0.10,
                         help="Points closer than this distance (metres) belong to the "
                              "same group. Default: 0.10")
    parser.add_argument("--floor-cell", type=float, default=0.5,
                         help="Size of the square cells (metres) in which the local floor "
                              "is found. Default: 0.5")
    args = parser.parse_args()
    if args.below_floor_margin <= 0:
        parser.error("--below-floor-margin must be larger than 0.")
    if args.below_floor_keep_area < 0 or args.below_floor_link <= 0 or args.floor_cell <= 0:
        parser.error("--below-floor-keep-area must be 0 or more; --below-floor-link and "
                     "--floor-cell must be larger than 0.")

    print(f"Loading: {args.input}")
    pcd = load_cloud(args.input)
    print(f"  {len(pcd.points)} points loaded.")

    print("Searching for candidate planes (floor/walls/ceiling)...")
    candidates = find_plane_candidates(
        pcd, max_planes=args.max_planes, distance_threshold=args.distance_threshold,
        min_inlier_fraction=args.min_inlier_fraction)

    if not candidates:
        print("ERROR: no planes found at all - the cloud may be too sparse or "
              "the distance threshold too tight. Try a larger --distance-threshold.")
        return 1

    for i, c in enumerate(candidates):
        horizontal_note = "horizontal candidate" if abs(c["normal"][2]) >= args.horizontal_threshold \
            else "NOT horizontal enough to be floor/ceiling"
        print(f"  Candidate {i}: {c['count']} points, normal={np.round(c['normal'], 3)}, "
              f"horizontality={abs(c['normal'][2]):.3f}, Z={c['centroid'][2]:.3f} ({horizontal_note})")

    floor, used_fallback = pick_floor_candidate(candidates, args.horizontal_threshold)
    if used_fallback:
        print(f"\nWARNING: no candidate cleared the horizontal threshold "
              f"({args.horizontal_threshold}) - falling back to the biggest-and-most-"
              f"horizontal score across ALL candidates, which can pick a wall by "
              f"mistake. Check the leveled result carefully, and consider a looser "
              f"--horizontal-threshold or a tighter --distance-threshold.")
    else:
        print(f"\nChosen as floor: the LOWEST of the horizontal candidates above "
              f"(picking the lowest, not the biggest, avoids a real ceiling - which "
              f"can have more points than the floor - winning instead).")
    print(f"  {floor['count']} points, normal={np.round(floor['normal'], 3)}, "
          f"Z={floor['centroid'][2]:.3f}")
    if abs(floor["normal"][2]) < 0.5:
        print("  WARNING: this candidate isn't very horizontal (|normal.z| < 0.5). "
              "It may actually be a wall, not the floor - check the leveled result "
              "carefully before trusting it. If wrong, the scan may need a tighter "
              "--distance-threshold or the tilt may be more extreme than this "
              "heuristic handles well.")

    pcd, R = level_point_cloud(pcd, floor)

    rotation_angle_deg = np.degrees(np.arccos(np.clip((np.trace(R) - 1) / 2, -1, 1)))
    print(f"\nApplied rotation: {rotation_angle_deg:.2f} degrees")
    print(f"Floor now at Z=0 by construction.")

    below = find_below_floor(np.asarray(pcd.points), margin=args.below_floor_margin,
                             cell=args.floor_cell, link=args.below_floor_link,
                             keep_area=args.below_floor_keep_area)
    print_below_floor_report(below, args.below_floor_margin, args.below_floor_keep_area,
                             args.remove_below_floor)
    if args.remove_below_floor and below["remove_mask"].any():
        n_before = len(pcd.points)
        pcd = pcd.select_by_index(np.flatnonzero(below["remove_mask"]), invert=True)
        print(f"  {n_before} points before, {len(pcd.points)} points after.")

    o3d.io.write_point_cloud(str(args.output), pcd)
    print(f"Saved to: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
