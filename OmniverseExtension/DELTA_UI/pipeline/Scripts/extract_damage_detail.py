#!/usr/bin/env python3
"""Extract inspection geometry near classified damage."""

import argparse
import sys

import numpy as np
from plyfile import PlyData, PlyElement

from cloud_io import find_distance_field, find_change_field


def load_flagged(path):
    print(f"Reading flagged points: {path}")
    ply = PlyData.read(str(path))
    if "vertex" not in ply:
        raise ValueError(f"'{path}' has no vertex data.")
    vertex = ply["vertex"]
    field_name = find_change_field(vertex)
    if field_name is None:
        raise ValueError(
            f"'{path}': no M3C2/distance field found. Available fields: "
            f"{list(vertex.data.dtype.names or ())}. Make sure this is "
            f"Stage 5's classified output, not a raw M3C2 result or an "
            f"unrelated cloud."
        )
    positions = np.stack([vertex["x"], vertex["y"], vertex["z"]], axis=-1).astype(
        np.float64
    )
    values = np.asarray(vertex[field_name], dtype=np.float64)
    names = vertex.data.dtype.names or ()
    cluster_ids = (
        np.asarray(vertex["cluster_id"], dtype=np.int32)
        if "cluster_id" in names
        else None
    )

    # A Classify run with --keep-all writes every point, with flag fields.
    # Use only the flagged points: cluster_id >= 0 (in a damage site), or
    # classified = 1 when there is no cluster_id. Without this, every
    # point of the surface counted as "flagged".
    if cluster_ids is not None:
        keep = cluster_ids >= 0
        rule = "cluster_id >= 0"
    elif "classified" in names:
        keep = np.asarray(vertex["classified"]) == 1
        rule = "classified = 1"
    else:
        keep = None
    if keep is not None and not keep.all():
        print(
            f"  {len(positions)} points in the file; using the {int(keep.sum())} "
            f"flagged point(s) ({rule})."
        )
        positions, values = positions[keep], values[keep]
        if cluster_ids is not None:
            cluster_ids = cluster_ids[keep]
    if not len(positions):
        raise ValueError(f"'{path}' has no flagged points - nothing to extract around.")

    print(
        f"  {len(positions)} flagged points, field '{field_name}'"
        + (", with cluster_id" if cluster_ids is not None else "")
    )
    return positions, values, cluster_ids


def load_comparison(path):
    print(f"Reading comparison cloud: {path}")
    ply = PlyData.read(str(path))
    if "vertex" not in ply:
        raise ValueError(f"'{path}' has no vertex data.")
    vertex = ply["vertex"]
    positions = np.stack([vertex["x"], vertex["y"], vertex["z"]], axis=-1).astype(
        np.float64
    )
    print(f"  {len(positions)} points")
    return positions


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--flagged",
        required=True,
        help="Stage 5's classified output (flagged points with the "
        "M3C2 distance field, positioned at baseline locations)",
    )
    parser.add_argument(
        "--comparison",
        required=True,
        help="The comparison cloud (e.g. Stage 3's cleaned comparison "
        "output) to extract real geometry from",
    )
    parser.add_argument("--output", required=True, help="Extracted detail .ply")
    parser.add_argument(
        "--radius",
        type=float,
        required=True,
        help="How far from each flagged point to pull comparison "
        "geometry from, in meters. Match this roughly to the "
        "Normal scale used in Stage 4's M3C2 run - that's the "
        "scale the measurement itself was already working at.",
    )
    args = parser.parse_args()

    try:
        flagged_pos, flagged_values, flagged_clusters = load_flagged(args.flagged)
        comparison_pos = load_comparison(args.comparison)
    except ValueError as e:
        print(f"ERROR: {e}")
        return 1

    try:
        from scipy.spatial import cKDTree
    except ImportError:
        print("ERROR: this script requires scipy ('pip install scipy').")
        return 1

    print(f"Searching within {args.radius} m of each flagged point...")
    tree = cKDTree(flagged_pos)
    dist, nearest_idx = tree.query(comparison_pos, k=1)
    keep_mask = dist <= args.radius

    n_kept = int(keep_mask.sum())
    print(
        f"  Extracted {n_kept} of {len(comparison_pos)} comparison points "
        f"({n_kept / len(comparison_pos) * 100:.2f}%)."
    )

    if n_kept == 0:
        print(
            "  WARNING: nothing extracted - try a larger --radius, or check that "
            "--flagged and --comparison are actually spatially aligned (same "
            "coordinate frame, both from the same Stage 3 alignment)."
        )

    extracted_pos = comparison_pos[keep_mask].astype(np.float32)
    carried_values = flagged_values[nearest_idx[keep_mask]].astype(np.float32)

    # No spaces: a PLY property name with a space cannot be written.
    field_name = "scalar_M3C2_distance"
    vertex_dtype = [("x", "f4"), ("y", "f4"), ("z", "f4"), (field_name, "f4")]
    if flagged_clusters is not None:
        vertex_dtype.append(("cluster_id", "i4"))
    vertex_data = np.zeros(len(extracted_pos), dtype=vertex_dtype)
    vertex_data["x"], vertex_data["y"], vertex_data["z"] = (
        extracted_pos[:, 0],
        extracted_pos[:, 1],
        extracted_pos[:, 2],
    )
    vertex_data[field_name] = carried_values
    if flagged_clusters is not None:
        vertex_data["cluster_id"] = flagged_clusters[nearest_idx[keep_mask]]

    element = PlyElement.describe(vertex_data, "vertex")
    PlyData([element], text=False).write(args.output)
    print(f"Saved to: {args.output}")
    print(
        "\nNext: use this file as --detail (or as --change, if replacing rather than "
        "supplementing the abstract highlight) when running usd_export.py, to add a "
        "true-geometry damage layer to the USD scene."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
