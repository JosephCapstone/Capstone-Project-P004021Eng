#!/usr/bin/env python3
"""
extract_damage_detail.py
===========================
Implements "Option C": pulls the ACTUAL comparison-cloud geometry near
flagged damage locations, instead of only showing a magnitude value
plotted at baseline positions (which is what Stage 5's classified output
gives you on its own, since core points are baseline-sourced).

Why this exists: Stage 5's M3C2 measurement uses baseline-sourced core
points (kept that way deliberately - it's what catches REMOVED material,
like a puncture or missing panel, which comparison-sourced core points
would miss entirely). But that means the flagged/classified points from
Stage 6 are positioned at baseline locations, not the comparison scan's
true current positions - so they can't show what added debris or a
deformed surface actually looks like right now.

This script bridges that gap without touching the M3C2 measurement at
all: it uses the flagged points purely as location markers ("damage
happened somewhere near here"), then extracts whatever comparison-cloud
points actually exist within --radius of any flagged point. That's real,
current geometry - true shape, true position - not a proxy value. Each
extracted point carries the damage type of the nearest flagged point (see
below), so the USD export can colour the real geometry by what kind of
damage it is.

Requires:
    pip install plyfile numpy scipy

Usage:
    python extract_damage_detail.py --flagged classified.ply \\
        --comparison comparison_cleaned.ply --output damage_detail.ply --radius 0.2

--flagged can be a Classify output with or without 'Keep all points'.
With all points, only the flagged ones are used: cluster_id >= 0, or
classified = 1 when there is no cluster_id, or change_type > 0 when there
is neither.

What each detail point carries (update 24)
------------------------------------------
Each detail point carries the value of the NEAREST flagged point. The best
value for damage is the CHANGE TYPE: it says what kind of damage the flagged
point belongs to, and it has the same meaning on every wall.

    change_type   0 none, 1 added, 2 removed, 3 hole, 4 unmatched

Stage 6 (Classify) writes change_type when "Find holes" is on. The value
is a category, not a measurement, so it is copied from the nearest flagged
point. It is never averaged. The magnitude of the change is not lost: it
stays in the field scalar_M3C2_distance (see below).

Output fields:
    x, y, z
    change_type            (i4) when --flagged has a change_type field and
                           --scalar is not 'distance'
    scalar_M3C2_distance   (f4) the magnitude of the nearest flagged point:
                           change_inward when --flagged has it (its sign has
                           the same meaning on every wall: + added, - removed,
                           and the points of a hole have a value), else the
                           M3C2 distance. Not written if --flagged has no
                           magnitude field at all (change_type only).
    cluster_id             (i4) when --flagged has a cluster_id field: the
                           damage site of the nearest flagged point.

usd_export.py colours /World/Compartment/DamageDetail by change_type when
the file has it (red added, blue removed, purple hole, teal unmatched, the
same colours as the DamageSites boxes). A file without change_type is
coloured by scalar_M3C2_distance, as before.

--scalar auto (the default) uses change_type when --flagged has it, and the
magnitude only when it has not. --scalar distance writes the old output
(magnitude only). --scalar change_type stops with an error when --flagged
has no change_type field.

The magnitude field is named scalar_M3C2_distance - the name CloudCompare
itself uses when it writes a PLY. A PLY property name cannot contain a
space, so the earlier name 'M3C2 distance' made the write fail with
"ValueError: space character(s) in name" every time (update 6 fix).
"""

import argparse
import sys

import numpy as np
from plyfile import PlyData, PlyElement

# change_type values written by m3c2_classify.py (Stage 6, "Find holes").
CHANGE_NONE, CHANGE_ADDED, CHANGE_REMOVED, CHANGE_HOLE, CHANGE_UNMATCHED = 0, 1, 2, 3, 4
CHANGE_NAMES = {CHANGE_NONE: "none", CHANGE_ADDED: "added", CHANGE_REMOVED: "removed",
                CHANGE_HOLE: "hole", CHANGE_UNMATCHED: "unmatched"}


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


def find_value_field(vertex):
    """The MAGNITUDE field to carry: 'change_inward' when the file has it
    (Stage 6 with "Find holes"), else the M3C2 distance field. None when the
    file has neither (it can still have change_type)."""
    if "change_inward" in (vertex.data.dtype.names or ()):
        return "change_inward"
    return find_distance_field(vertex)


def load_flagged(path, scalar="auto"):
    """Reads the flagged points. Returns a dict:
        positions   Nx3 float64
        magnitude   N float64, or None (no magnitude field in the file)
        magnitude_field  its field name, or None
        change_type N int32, or None (no change_type field, or scalar='distance')
        cluster_ids N int32, or None
    Raises ValueError with a clear text when the file cannot be used."""
    print(f"Reading flagged points: {path}")
    ply = PlyData.read(str(path))
    if "vertex" not in ply:
        raise ValueError(f"'{path}' has no vertex data.")
    vertex = ply["vertex"]
    names = vertex.data.dtype.names or ()
    magnitude_field = find_value_field(vertex)
    has_type = "change_type" in names

    if scalar == "change_type" and not has_type:
        raise ValueError(
            f"'{path}' has no 'change_type' field, so --scalar change_type cannot be "
            f"used. Run Stage 6 (Classify) with 'Find holes' on, or use --scalar auto. "
            f"Available fields: {list(names)}")
    use_type = has_type and scalar in ("auto", "change_type")
    if magnitude_field is None and not use_type:
        raise ValueError(
            f"'{path}': no change_type and no M3C2/distance field found. Available "
            f"fields: {list(names)}. Make sure this is Stage 6's classified output, "
            f"not a raw M3C2 result or an unrelated cloud.")

    positions = np.stack([vertex["x"], vertex["y"], vertex["z"]], axis=-1).astype(np.float64)
    magnitude = (np.asarray(vertex[magnitude_field], dtype=np.float64)
                 if magnitude_field is not None else None)
    change_type = (np.asarray(vertex["change_type"]).astype(np.int32) if use_type else None)
    cluster_ids = np.asarray(vertex["cluster_id"], dtype=np.int32) if "cluster_id" in names else None

    # A Classify run with --keep-all writes every point, with flag fields.
    # Use only the flagged points: cluster_id >= 0 (in a damage site), or
    # classified = 1 when there is no cluster_id, or change_type > 0 when
    # there is neither. Without this, every point of the surface counted as
    # "flagged".
    if cluster_ids is not None:
        keep = cluster_ids >= 0
        rule = "cluster_id >= 0"
    elif "classified" in names:
        keep = np.asarray(vertex["classified"]) == 1
        rule = "classified = 1"
    elif has_type:
        keep = np.asarray(vertex["change_type"]) > 0
        rule = "change_type > 0"
    else:
        keep = None
    if keep is not None and not keep.all():
        print(f"  {len(positions)} points in the file; using the {int(keep.sum())} "
              f"flagged point(s) ({rule}).")
        positions = positions[keep]
        if magnitude is not None:
            magnitude = magnitude[keep]
        if change_type is not None:
            change_type = change_type[keep]
        if cluster_ids is not None:
            cluster_ids = cluster_ids[keep]
    if not len(positions):
        raise ValueError(f"'{path}' has no flagged points - nothing to extract around.")

    carried = []
    if change_type is not None:
        carried.append("'change_type'")
    if magnitude is not None:
        carried.append(f"'{magnitude_field}'")
    print(f"  {len(positions)} flagged points, carrying {' and '.join(carried)}"
          + (", with cluster_id" if cluster_ids is not None else ""))
    if change_type is not None:
        print("  Flagged points by change type: " + describe_types(change_type))
    elif has_type and scalar == "distance":
        print("  NOTE: the file has change_type, but --scalar distance was given. "
              "The detail file will have the magnitude only.")
    else:
        print("  NOTE: the file has no change_type field (Stage 6 ran without 'Find holes'). "
              "The detail points carry the magnitude only, and the USD export colours "
              "them by the magnitude.")
    return {"positions": positions, "magnitude": magnitude,
            "magnitude_field": magnitude_field, "change_type": change_type,
            "cluster_ids": cluster_ids}


def describe_types(change_type):
    """'added 120, removed 40' - the change types in an array, with counts.
    Types with no point are not listed."""
    parts = []
    for value, name in CHANGE_NAMES.items():
        count = int((change_type == value).sum())
        if count:
            parts.append(f"{name} {count}")
    unknown = int((~np.isin(change_type, list(CHANGE_NAMES))).sum())
    if unknown:
        parts.append(f"unknown {unknown}")
    return ", ".join(parts) if parts else "none"


def load_comparison(path):
    print(f"Reading comparison cloud: {path}")
    ply = PlyData.read(str(path))
    if "vertex" not in ply:
        raise ValueError(f"'{path}' has no vertex data.")
    vertex = ply["vertex"]
    positions = np.stack([vertex["x"], vertex["y"], vertex["z"]], axis=-1).astype(np.float64)
    print(f"  {len(positions)} points")
    return positions


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--flagged", required=True,
                         help="Stage 6's classified output (flagged points, positioned at "
                              "baseline locations). Best: a run with 'Find holes' on, so "
                              "the file has change_type.")
    parser.add_argument("--comparison", required=True,
                         help="The comparison cloud (e.g. Stage 3's cleaned comparison "
                              "output) to extract real geometry from")
    parser.add_argument("--output", required=True, help="Extracted detail .ply")
    parser.add_argument("--radius", type=float, required=True,
                         help="How far from each flagged point to pull comparison "
                              "geometry from, in meters. Match this roughly to the "
                              "Normal scale used in Stage 5's M3C2 run - that's the "
                              "scale the measurement itself was already working at.")
    parser.add_argument("--scalar", choices=("auto", "change_type", "distance"),
                         default="auto",
                         help="What the detail points carry. auto (default): the change "
                              "type (1 added, 2 removed, 3 hole, 4 unmatched) when --flagged "
                              "has it, else the magnitude. change_type: the change type, "
                              "and an error when --flagged has none. distance: the "
                              "magnitude only (the output of update 23 and earlier). The "
                              "magnitude is written as scalar_M3C2_distance in every case "
                              "where the file has one.")
    args = parser.parse_args()

    try:
        flagged = load_flagged(args.flagged, scalar=args.scalar)
        comparison_pos = load_comparison(args.comparison)
    except ValueError as e:
        print(f"ERROR: {e}")
        return 1

    try:
        from scipy.spatial import cKDTree
    except ImportError:
        print("ERROR: this script requires scipy ('pip install scipy').")
        return 1

    # The radius is measured from the flagged point, which sits at the BASELINE
    # position. Material that moved farther than the radius is not found.
    magnitude = flagged["magnitude"]
    if magnitude is not None:
        measured = np.isfinite(magnitude)
        if flagged["change_type"] is not None:
            measured &= np.isin(flagged["change_type"], (CHANGE_ADDED, CHANGE_REMOVED))
        if measured.any():
            largest = float(np.max(np.abs(magnitude[measured])))
            if largest > args.radius:
                print(f"  NOTE: the largest measured change is {largest * 100:.1f} cm, and the "
                      f"radius is {args.radius * 100:.1f} cm. Material that moved farther than "
                      f"the radius from its baseline position is not extracted. Use a radius "
                      f"larger than {largest * 100:.1f} cm to get all of it.")

    print(f"Searching within {args.radius} m of each flagged point...")
    tree = cKDTree(flagged["positions"])
    dist, nearest_idx = tree.query(comparison_pos, k=1)
    keep_mask = dist <= args.radius
    nearest_kept = nearest_idx[keep_mask]

    n_kept = int(keep_mask.sum())
    print(f"  Extracted {n_kept} of {len(comparison_pos)} comparison points "
          f"({n_kept / len(comparison_pos) * 100:.2f}%)." if len(comparison_pos) else
          "  The comparison cloud has no points.")

    if n_kept == 0:
        print("  WARNING: nothing extracted - try a larger --radius, or check that "
              "--flagged and --comparison are actually spatially aligned (same "
              "coordinate frame, both from the same Stage 3 alignment).")

    extracted_pos = comparison_pos[keep_mask].astype(np.float32)

    # No spaces in a field name: a PLY property name with a space cannot be written.
    magnitude_name = "scalar_M3C2_distance"
    vertex_dtype = [("x", "f4"), ("y", "f4"), ("z", "f4")]
    if flagged["change_type"] is not None:
        vertex_dtype.append(("change_type", "i4"))
    if flagged["magnitude"] is not None:
        vertex_dtype.append((magnitude_name, "f4"))
    if flagged["cluster_ids"] is not None:
        vertex_dtype.append(("cluster_id", "i4"))
    vertex_data = np.zeros(len(extracted_pos), dtype=vertex_dtype)
    vertex_data["x"], vertex_data["y"], vertex_data["z"] = \
        extracted_pos[:, 0], extracted_pos[:, 1], extracted_pos[:, 2]
    # Each value is COPIED from the nearest flagged point. The change type is
    # a category, so it is never averaged or interpolated.
    if flagged["change_type"] is not None:
        vertex_data["change_type"] = flagged["change_type"][nearest_kept]
    if flagged["magnitude"] is not None:
        vertex_data[magnitude_name] = flagged["magnitude"][nearest_kept].astype(np.float32)
    if flagged["cluster_ids"] is not None:
        vertex_data["cluster_id"] = flagged["cluster_ids"][nearest_kept]

    if flagged["change_type"] is not None and n_kept:
        print("  Detail points by change type: "
              + describe_types(vertex_data["change_type"]))
    if flagged["cluster_ids"] is not None:
        # A damage site with no detail point. For a hole this is normal: the
        # comparison scan has no points there.
        sites_without_detail = (set(flagged["cluster_ids"].tolist())
                                - set(vertex_data["cluster_id"].tolist()))
        if sites_without_detail:
            ids = ", ".join(str(i) for i in sorted(sites_without_detail))
            print(f"  NOTE: no comparison point is within the radius of damage site(s) {ids}. "
                  f"This is normal for a hole (the comparison scan has no points there). "
                  f"For another type, try a larger --radius.")

    element = PlyElement.describe(vertex_data, "vertex")
    PlyData([element], text=False).write(args.output)
    print(f"Saved to: {args.output}")
    print("\nNext: use this file as --detail when running usd_export.py, to add a "
          "true-geometry damage layer to the USD scene"
          + (" (coloured by change type)." if flagged["change_type"] is not None
             else " (coloured by the magnitude)."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
