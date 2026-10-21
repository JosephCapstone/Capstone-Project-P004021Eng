#!/usr/bin/env python3
"""Estimate point spacing for comparison settings."""

import argparse
import sys

import numpy as np
import open3d as o3d


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--input", required=True, help="Point cloud .ply")
    parser.add_argument(
        "--sample-size",
        type=int,
        default=200000,
        help="Max points to sample for the spacing calculation, for "
        "speed on very large clouds. Default: 200000",
    )
    args = parser.parse_args()

    print(f"Loading: {args.input}")
    pcd = o3d.io.read_point_cloud(str(args.input))
    n = len(pcd.points)
    if n == 0:
        print("ERROR: cloud has zero points.")
        return 1
    print(f"  {n} points loaded.")

    if n > args.sample_size:
        print(f"  Sampling {args.sample_size} points for speed...")
        idx = np.random.choice(n, args.sample_size, replace=False)
        pcd = pcd.select_by_index(idx)

    print("Computing nearest-neighbor distances...")
    distances = np.asarray(pcd.compute_nearest_neighbor_distance())

    mean_spacing = float(np.mean(distances))
    median_spacing = float(np.median(distances))
    p10 = float(np.percentile(distances, 10))
    p90 = float(np.percentile(distances, 90))

    print(f"\n  Mean spacing:   {mean_spacing:.5f}")
    print(f"  Median spacing: {median_spacing:.5f}")
    print(f"  10th pctile:    {p10:.5f}  (denser regions)")
    print(f"  90th pctile:    {p90:.5f}  (sparser regions)")

    low = median_spacing * 5
    high = median_spacing * 10
    print(f"\n  Suggested M3C2 normal diameter range: {low:.4f} to {high:.4f}")
    print(
        "  (roughly 5-10x median spacing - below this, normals get noisy from "
        "insufficient neighbors; above this, small features/edges get smoothed over)"
    )

    if p90 > median_spacing * 3:
        print(
            "\n  NOTE: spacing varies a lot across this cloud (90th percentile is "
            "3x+ the median) - density isn't uniform, so a single normal diameter "
            "won't be equally appropriate everywhere. Sparser regions may still "
            "show noisy normals even within the suggested range above."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
