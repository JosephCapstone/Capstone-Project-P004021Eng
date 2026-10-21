#!/usr/bin/env python3
"""Level point clouds using detected floor planes."""

import argparse
import sys

import numpy as np
import open3d as o3d

from cloud_io import load_cloud


def find_plane_candidates(
    pcd,
    max_planes=6,
    distance_threshold=0.02,
    ransac_n=3,
    num_iterations=1000,
    min_inlier_fraction=0.02,
):
    """Repeatedly finds the largest remaining plane and removes its inliers, building a short list of candidate planes (floor, ceiling, walls all show up as candidates here - picking which one is the floor happens in the."""
    remaining = pcd
    total_points = len(pcd.points)
    candidates = []

    for _ in range(max_planes):
        if len(remaining.points) < ransac_n * 2:
            break
        plane_model, inliers = remaining.segment_plane(
            distance_threshold, ransac_n, num_iterations
        )
        if len(inliers) < total_points * min_inlier_fraction:
            break

        a, b, c, d = plane_model
        normal = np.array([a, b, c], dtype=np.float64)
        normal /= np.linalg.norm(normal)
        inlier_points = np.asarray(remaining.select_by_index(inliers).points)

        candidates.append(
            {
                "normal": normal,
                "d": d,
                "count": len(inliers),
                "centroid": inlier_points.mean(axis=0),
            }
        )
        remaining = remaining.select_by_index(inliers, invert=True)

    return candidates


def pick_floor_candidate(candidates, horizontal_threshold=0.7):
    """Among candidates that are actually near-horizontal (|normal.z| >= horizontal_threshold - this alone already rules out walls), picks the LOWEST one by Z centroid as the floor."""
    if not candidates:
        return None, False

    horizontal_candidates = [
        c for c in candidates if abs(c["normal"][2]) >= horizontal_threshold
    ]
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
        vx = np.array(
            [[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]]
        )
        return np.eye(3) + 2 * (vx @ vx)  # Rodrigues, 180 degree case

    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * ((1 - c) / (s**2))


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


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--input", required=True, help="Raw SLAM output .ply")
    parser.add_argument("--output", required=True, help="Leveled output .ply")
    parser.add_argument(
        "--distance-threshold",
        type=float,
        default=0.02,
        help="RANSAC plane-fit tolerance, in the cloud's units "
        "(usually meters). Default: 0.02",
    )
    parser.add_argument(
        "--max-planes",
        type=int,
        default=6,
        help="How many candidate planes to search through before "
        "giving up on finding a floor. Default: 6",
    )
    parser.add_argument(
        "--min-inlier-fraction",
        type=float,
        default=0.02,
        help="A candidate plane must contain at least this fraction "
        "of all points to count (0.02 = 2%%). Lower this if a "
        "real floor/wall is smaller relative to the whole cloud "
        "than usual. Default: 0.02",
    )
    parser.add_argument(
        "--horizontal-threshold",
        type=float,
        default=0.7,
        help="|normal.z| above this counts as a floor/ceiling "
        "candidate rather than a wall - only candidates that "
        "clear this bar are eligible to be picked as the floor "
        "at all. Same default as segment_planes.py's own "
        "horizontal threshold, for consistency. Default: 0.7",
    )
    args = parser.parse_args()

    print(f"Loading: {args.input}")
    pcd = load_cloud(args.input)
    print(f"  {len(pcd.points)} points loaded.")

    print("Searching for candidate planes (floor/walls/ceiling)...")
    candidates = find_plane_candidates(
        pcd,
        max_planes=args.max_planes,
        distance_threshold=args.distance_threshold,
        min_inlier_fraction=args.min_inlier_fraction,
    )

    if not candidates:
        print(
            "ERROR: no planes found at all - the cloud may be too sparse or "
            "the distance threshold too tight. Try a larger --distance-threshold."
        )
        return 1

    for i, c in enumerate(candidates):
        horizontal_note = (
            "horizontal candidate"
            if abs(c["normal"][2]) >= args.horizontal_threshold
            else "NOT horizontal enough to be floor/ceiling"
        )
        print(
            f"  Candidate {i}: {c['count']} points, normal={np.round(c['normal'], 3)}, "
            f"horizontality={abs(c['normal'][2]):.3f}, Z={c['centroid'][2]:.3f} ({horizontal_note})"
        )

    floor, used_fallback = pick_floor_candidate(candidates, args.horizontal_threshold)
    if used_fallback:
        print(
            f"\nWARNING: no candidate cleared the horizontal threshold "
            f"({args.horizontal_threshold}) - falling back to the biggest-and-most-"
            f"horizontal score across ALL candidates, which can pick a wall by "
            f"mistake. Check the leveled result carefully, and consider a looser "
            f"--horizontal-threshold or a tighter --distance-threshold."
        )
    else:
        print(
            f"\nChosen as floor: the LOWEST of the horizontal candidates above "
            f"(picking the lowest, not the biggest, avoids a real ceiling - which "
            f"can have more points than the floor - winning instead)."
        )
    print(
        f"  {floor['count']} points, normal={np.round(floor['normal'], 3)}, "
        f"Z={floor['centroid'][2]:.3f}"
    )
    if abs(floor["normal"][2]) < 0.5:
        print(
            "  WARNING: this candidate isn't very horizontal (|normal.z| < 0.5). "
            "It may actually be a wall, not the floor - check the leveled result "
            "carefully before trusting it. If wrong, the scan may need a tighter "
            "--distance-threshold or the tilt may be more extreme than this "
            "heuristic handles well."
        )

    pcd, R = level_point_cloud(pcd, floor)

    rotation_angle_deg = np.degrees(np.arccos(np.clip((np.trace(R) - 1) / 2, -1, 1)))
    print(f"\nApplied rotation: {rotation_angle_deg:.2f} degrees")
    print(f"Floor now at Z=0 by construction.")

    o3d.io.write_point_cloud(str(args.output), pcd)
    print(f"Saved to: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
