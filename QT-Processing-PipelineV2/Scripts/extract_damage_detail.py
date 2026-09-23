#!/usr/bin/env python3
"""
extract_damage_detail.py
===========================
Implements "Option C": pulls the ACTUAL comparison-cloud geometry near
flagged damage locations, instead of only showing a magnitude value
plotted at baseline positions (which is what Stage 5's classified output
gives you on its own, since core points are baseline-sourced).

Why this exists: Stage 4's M3C2 measurement uses baseline-sourced core
points (kept that way deliberately - it's what catches REMOVED material,
like a puncture or missing panel, which comparison-sourced core points
would miss entirely). But that means the flagged/classified points from
Stage 5 are positioned at baseline locations, not the comparison scan's
true current positions - so they can't show what added debris or a
deformed surface actually looks like right now.

This script bridges that gap without touching the M3C2 measurement at
all: it uses the flagged points purely as location markers ("damage
happened somewhere near here"), then extracts whatever comparison-cloud
points actually exist within --radius of any flagged point. That's real,
current geometry - true shape, true position - not a proxy value. Each
extracted point carries the nearest flagged point's M3C2 distance value,
so it can still use the same diverging colormap as ChangeHighlight in
USD, keeping the visual language consistent between the two layers.

Requires:
    pip install plyfile numpy scipy

Usage:
    python extract_damage_detail.py --flagged classified.ply \\
        --comparison comparison_cleaned.ply --output damage_detail.ply --radius 0.2

STATUS: newly written, not yet run against real data.
"""

import argparse
import sys

import numpy as np
from plyfile import PlyData, PlyElement


def find_distance_field(vertex):
    """Same detection logic as m3c2_classify.py / usd_export.py - kept
    duplicated intentionally, consistent with this project's convention
    of standalone scripts not importing from each other."""
    field_names = vertex.data.dtype.names or ()
    normalized = {n: n.lower().replace("_", " ").strip() for n in field_names}
    for n, norm in normalized.items():
        if norm == "m3c2 distance":
            return n

    m3c2_candidates = [n for n in field_names
                        if "m3c2" in n.lower() and "uncertain" not in n.lower()
                        and "signif" not in n.lower()]
    if m3c2_candidates:
        return m3c2_candidates[0]

    distance_candidates = [n for n in field_names
                            if "distance" in n.lower() and "uncertain" not in n.lower()]
    if distance_candidates:
        return distance_candidates[0]

    fallback = [n for n in field_names if "distance" in n.lower()]
    return fallback[0] if fallback else None


def load_flagged(path):
    print(f"Reading flagged points: {path}")
    ply = PlyData.read(str(path))
    if "vertex" not in ply:
        raise ValueError(f"'{path}' has no vertex data.")
    vertex = ply["vertex"]
    field_name = find_distance_field(vertex)
    if field_name is None:
        raise ValueError(
            f"'{path}': no M3C2/distance field found. Available fields: "
            f"{list(vertex.data.dtype.names or ())}. Make sure this is "
            f"Stage 5's classified output, not a raw M3C2 result or an "
            f"unrelated cloud."
        )
    positions = np.stack([vertex["x"], vertex["y"], vertex["z"]], axis=-1).astype(np.float64)
    values = np.asarray(vertex[field_name], dtype=np.float64)
    print(f"  {len(positions)} flagged points, field '{field_name}'")
    return positions, values


def load_comparison(path):
    print(f"Reading comparison cloud: {path}")
    ply = PlyData.read(str(path))
    if "vertex" not in ply:
        raise ValueError(f"'{path}' has no vertex data.")
    vertex = ply["vertex"]
    positions = np.stack([vertex["x"], vertex["y"], vertex["z"]], axis=-1).astype(np.float64)
    if len(positions) == 0:
        raise ValueError(
            f"'{path}' loaded but has zero points - nothing to extract geometry "
            f"from. Check this is the right file (Stage 3's cleaned comparison "
            f"output), not an empty or wrong-stage cloud."
        )
    print(f"  {len(positions)} points")
    return positions


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--flagged", required=True,
                         help="Stage 5's classified output (flagged points with the "
                              "M3C2 distance field, positioned at baseline locations)")
    parser.add_argument("--comparison", required=True,
                         help="The comparison cloud (e.g. Stage 3's cleaned comparison "
                              "output) to extract real geometry from")
    parser.add_argument("--output", required=True, help="Extracted detail .ply")
    parser.add_argument("--radius", type=float, required=True,
                         help="How far from each flagged point to pull comparison "
                              "geometry from, in meters. Match this roughly to the "
                              "Normal scale used in Stage 4's M3C2 run - that's the "
                              "scale the measurement itself was already working at.")
    args = parser.parse_args()

    flagged_pos, flagged_values = load_flagged(args.flagged)
    comparison_pos = load_comparison(args.comparison)

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
    print(f"  Extracted {n_kept} of {len(comparison_pos)} comparison points "
          f"({n_kept / len(comparison_pos) * 100:.2f}%).")

    if n_kept == 0:
        print("  WARNING: nothing extracted - try a larger --radius, or check that "
              "--flagged and --comparison are actually spatially aligned (same "
              "coordinate frame, both from the same Stage 3 alignment).")

    extracted_pos = comparison_pos[keep_mask].astype(np.float32)
    carried_values = flagged_values[nearest_idx[keep_mask]].astype(np.float32)

    # Underscore, not a literal space: a PLY property line is
    # "property <type> <name>", whitespace-delimited, so a space inside
    # the name breaks the header format. Matches the on-disk convention
    # CloudCompare itself already uses for this exact field, which is
    # why find_distance_field() (here, in m3c2_classify.py, and in
    # usd_export.py) normalizes underscores back to spaces to find it -
    # a downstream reader looking for "M3C2 distance" will find this
    # field correctly via that same normalization.
    field_name = "M3C2_distance"
    vertex_dtype = [("x", "f4"), ("y", "f4"), ("z", "f4"), (field_name, "f4")]
    vertex_data = np.zeros(len(extracted_pos), dtype=vertex_dtype)
    vertex_data["x"], vertex_data["y"], vertex_data["z"] = \
        extracted_pos[:, 0], extracted_pos[:, 1], extracted_pos[:, 2]
    vertex_data[field_name] = carried_values

    element = PlyElement.describe(vertex_data, "vertex")
    PlyData([element], text=False).write(args.output)
    print(f"Saved to: {args.output}")
    print("\nNext: use this file as --detail (or as --change, if replacing rather than "
          "supplementing the abstract highlight) when running usd_export.py, to add a "
          "true-geometry damage layer to the USD scene.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
