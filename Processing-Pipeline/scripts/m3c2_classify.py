#!/usr/bin/env python3
"""
m3c2_classify.py
==================
Takes the raw M3C2 diff result (a point per core point, each with a signed
distance value) and reduces it to just the points that represent real
change - filtering out the noise floor so the change-highlight cloud fed
into Stage 6 (Export) actually shows damage/debris, not the whole surface.

Two filtering passes, run in order:

  Step A (magnitude): a point's M3C2 distance must clear the RMS-based
  --threshold to be flagged at all. This is the original, validated
  behavior and is completely unchanged by everything below.

  Step B/C (spatial, on by default - see --no-cluster): the flagged
  points from Step A are then clustered by 3D position (DBSCAN or
  HDBSCAN). A flagged point with no nearby flagged neighbors ("noise", in
  the clustering sense) is exactly the profile of sensor noise or a
  registration artifact, not real damage - so it gets treated as a
  second, independent false-positive filter, layered on top of Step A
  rather than replacing it. Real damage should be corroborated both
  statistically (Step A) and spatially (Step B/C).

Step D turns the surviving clusters into a per-site summary (centroid,
point count, bounding extent, mean/max M3C2 magnitude) - the difference
between "14,000 flagged points" and "3 damage sites, sizes X/Y/Z". This
summary is written as a sidecar JSON file next to --output, named by
swapping the output file's extension for '.clusters.json' (e.g.
classified.ply -> classified.clusters.json), the same manifest.json-style
record segment_planes.py writes for its own output (Section 3.3 of the
project schema - a project-mode caller absorbs this file's content into
project.json instead of needing to read it directly).

Requires:
    pip install plyfile numpy scikit-learn

Usage:
    python m3c2_classify.py --input diff_result.ply --output classified.ply --threshold 0.02
    python m3c2_classify.py --input diff_result.ply --output classified.ply --threshold 0.02 \\
        --cluster-method hdbscan --min-cluster-size 6
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from plyfile import PlyData, PlyElement


def find_distance_field(vertex):
    """
    Finds CloudCompare's M3C2 signed-distance field, not its uncertainty
    field - both contain 'distance' in the name (e.g. 'M3C2 distance' vs
    'distance_uncertainty'), and a naive substring search on 'distance'
    matches the wrong one, silently. Uncertainty values are always
    positive with no meaningful zero-crossing, so thresholding that field
    instead of the real one flags nearly everything as "changed" - this
    is what happened on a real run and is why the exclusion below exists.

    Priority order:
    1. Exact match for CloudCompare's standard name 'M3C2 distance'
       (case/spacing-insensitive)
    2. Any 'm3c2' field that isn't an uncertainty/significance field
    3. Any 'distance' field that isn't an uncertainty field
    4. Last resort: any field with 'distance' in the name at all
    """
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


def cluster_flagged_points(points, method, eps, min_samples, min_cluster_size):
    """
    Clusters just the flagged (Step A) points by 3D position. Returns an
    int64 label per input point: -1 means "noise" (no cluster - either
    DBSCAN/HDBSCAN's own density test failed, or the cluster it landed in
    didn't meet --min-cluster-size), 0..N-1 are dense, contiguous cluster
    ids for the surviving clusters.

    method='dbscan': classic fixed-radius density clustering, the same
    algorithm already validated on this sensor/environment for plane
    segmentation (segment_planes.py). Requires one --cluster-eps value to
    work everywhere in the flagged-point set. --min-cluster-size is
    applied here as an explicit second pass, since DBSCAN itself has no
    native "minimum final cluster size" concept beyond --cluster-min-samples
    (the density parameter, not a size floor).

    method='hdbscan': builds a hierarchy across a range of density
    thresholds and picks stable clusters automatically - handles
    variable-density flagged-point sets (which M3C2 core point density
    can produce, depending on surface angle/distance from scanner) without
    needing a single --cluster-eps to be right everywhere. --min-cluster-size
    is passed straight to HDBSCAN, which already enforces it natively.
    """
    if method == "hdbscan":
        from sklearn.cluster import HDBSCAN
        clusterer = HDBSCAN(min_cluster_size=max(int(min_cluster_size), 2),
                             min_samples=min_samples, copy=True)
        raw_labels = clusterer.fit_predict(points)
    else:
        from sklearn.cluster import DBSCAN
        clusterer = DBSCAN(eps=eps, min_samples=min_samples)
        raw_labels = clusterer.fit_predict(points)

    labels = raw_labels.astype(np.int64).copy()

    if method == "dbscan" and min_cluster_size > 0:
        unique, counts = np.unique(labels[labels >= 0], return_counts=True)
        too_small = unique[counts < min_cluster_size]
        if len(too_small):
            labels[np.isin(labels, too_small)] = -1

    # Renumber surviving cluster ids densely from 0, so ids stay
    # contiguous even after the too-small pass above drops some.
    surviving = sorted(set(labels[labels >= 0].tolist()))
    remap = {old: new for new, old in enumerate(surviving)}
    final_labels = np.array([remap.get(int(l), -1) for l in labels], dtype=np.int64)
    return final_labels


def summarize_clusters(flagged_points, flagged_distances, labels):
    """
    Step D: turns per-point cluster labels into one aggregate record per
    surviving cluster - centroid, point count, bounding extent (max-min
    per axis), mean and max M3C2 magnitude. This is what a report or
    operator-facing summary actually wants ("3 damage sites, sizes
    X/Y/Z"), not a raw list of thousands of flagged points.
    """
    clusters = []
    n_clusters = int(labels.max()) + 1 if len(labels) and labels.max() >= 0 else 0
    for cluster_id in range(n_clusters):
        mask = labels == cluster_id
        pts = flagged_points[mask]
        mags = np.abs(flagged_distances[mask])
        mins = pts.min(axis=0)
        maxs = pts.max(axis=0)
        clusters.append({
            "cluster_id": cluster_id,
            "point_count": int(mask.sum()),
            "centroid": pts.mean(axis=0).tolist(),
            "extent": (maxs - mins).tolist(),
            "mean_magnitude": float(mags.mean()),
            "max_magnitude": float(mags.max()),
        })
    return clusters


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, help="M3C2 diff result .ply")
    parser.add_argument("--output", required=True, help="Filtered/classified .ply")
    parser.add_argument("--threshold", type=float, required=True,
                         help="Absolute distance (same units as the point cloud, "
                              "usually meters) above which a point counts as real "
                              "change. Points below this are dropped as noise.")
    parser.add_argument("--keep-all", action="store_true",
                         help="Don't drop below-threshold or below-cluster points - "
                              "instead keep everything and add 'classified' (Step A: "
                              "1=passed the magnitude threshold, 0=did not) and, if "
                              "clustering is on, 'cluster_id' (Step B/C: -1=not part "
                              "of a surviving cluster, 0..N-1=cluster membership) "
                              "fields. Useful if you want to inspect either filter's "
                              "effect in CloudCompare before committing.")
    parser.add_argument("--no-cluster", dest="cluster", action="store_false",
                         help="Disable spatial clustering (on by default). Without "
                              "this, Step A's magnitude threshold is the only filter "
                              "applied, matching the tool's original behavior - no "
                              "'cluster_id' field, no *.clusters.json sidecar file.")
    parser.add_argument("--cluster-method", choices=["dbscan", "hdbscan"], default="dbscan",
                         help="[cluster] dbscan: fixed-radius density clustering, "
                              "already validated on this sensor/environment for plane "
                              "segmentation - needs one --cluster-eps to work "
                              "everywhere in the flagged-point set. hdbscan: adapts to "
                              "variable density automatically, worth trying if flagged-"
                              "point density varies a lot by surface angle/distance "
                              "from scanner. Default: dbscan")
    parser.add_argument("--cluster-eps", type=float, default=0.05,
                         help="[cluster, dbscan only] Max gap (meters) between flagged "
                              "points to still count as the same damage site. M3C2 core "
                              "point spacing is usually finer than a plane-segmentation "
                              "cloud's, so this defaults tighter than segment_planes.py's "
                              "--cluster-eps (0.15) - check core point spacing "
                              "(point_spacing.py) if sites are splitting or merging "
                              "unexpectedly. Default: 0.05")
    parser.add_argument("--cluster-min-samples", type=int, default=4,
                         help="[cluster] DBSCAN/HDBSCAN's density parameter: how many "
                              "flagged neighbors (within --cluster-eps, for dbscan) a "
                              "point needs to seed a cluster at all. Default: 4")
    parser.add_argument("--min-cluster-size", type=int, default=4,
                         help="[cluster] A cluster's own tunable size floor, separate "
                              "from --cluster-min-samples - e.g. 'require at least 4 "
                              "flagged points total to count as a real site,' an "
                              "explicit, inspectable rejection criterion on top of "
                              "whatever --cluster-min-samples produced. Clusters "
                              "smaller than this are folded into noise (cluster_id=-1) "
                              "and dropped by Step C, same as any other noise point. "
                              "Default: 4")
    args = parser.parse_args()

    print(f"Reading: {args.input}")
    ply = PlyData.read(args.input)
    if "vertex" not in ply:
        print("ERROR: no vertex data found - is this really a point cloud PLY?")
        return 1
    vertex = ply["vertex"]

    field_name = find_distance_field(vertex)
    if field_name is None:
        print(f"ERROR: no M3C2/distance field found. Available fields: "
              f"{list(vertex.data.dtype.names or ())}")
        print("Make sure --input points at the actual M3C2 result file, not one "
              "of the duplicate input-cloud copies CloudCompare also saves.")
        return 1
    print(f"  Available fields: {list(vertex.data.dtype.names or ())}")
    print(f"  Selected field: '{field_name}'")

    distances = np.asarray(vertex[field_name], dtype=np.float64)
    finite_mask = np.isfinite(distances)
    n_total = len(distances)
    n_invalid = int((~finite_mask).sum())
    if n_invalid:
        print(f"  Note: {n_invalid} points had no valid M3C2 distance (outside "
              f"max distance / insufficient neighbors) and are treated as unchanged.")

    finite_distances = distances[finite_mask]
    if len(finite_distances) and (finite_distances >= 0).all():
        print("  WARNING: every value in the selected field is >= 0. A real M3C2 "
              "signed distance should straddle zero on unchanged surfaces - an "
              "all-positive field usually means this is actually an uncertainty "
              "or magnitude field, not the real distance field. Double check "
              "'Selected field' above against what you expect, and check the "
              "'Available fields' list for a better match if this looks wrong.")

    # --- Step A: magnitude threshold (unchanged) -------------------------
    significant = finite_mask & (np.abs(distances) >= args.threshold)
    n_significant = int(significant.sum())
    pct = (n_significant / n_total * 100) if n_total else 0.0

    print(f"  Field used: '{field_name}'")
    print(f"  Threshold: {args.threshold}")
    print(f"  Total points: {n_total}")
    print(f"  Flagged as changed (Step A): {n_significant} ({pct:.2f}%)")
    if n_significant:
        flagged_vals = distances[significant]
        print(f"  Flagged distance range: {flagged_vals.min():.4f} to {flagged_vals.max():.4f}")
        print(f"  Flagged distance mean magnitude: {np.mean(np.abs(flagged_vals)):.4f}")
    else:
        print("  WARNING: no points passed the threshold. Either nothing changed "
              "significantly, or --threshold is set too high for this scan.")

    # --- Step B/C: spatial clustering of the flagged subset --------------
    cluster_id = np.full(n_total, -1, dtype=np.int64)
    clusters_summary = []
    n_noise = 0
    n_confirmed = 0

    if args.cluster and n_significant > 0:
        method_label = args.cluster_method
        print(f"\nClustering {n_significant} flagged points (method={method_label})...")
        flagged_indices = np.where(significant)[0]
        xs = np.asarray(vertex["x"], dtype=np.float64)
        ys = np.asarray(vertex["y"], dtype=np.float64)
        zs = np.asarray(vertex["z"], dtype=np.float64)
        flagged_points = np.column_stack([xs[flagged_indices], ys[flagged_indices],
                                           zs[flagged_indices]])
        flagged_distances = distances[flagged_indices]

        labels = cluster_flagged_points(
            flagged_points, method_label, args.cluster_eps,
            args.cluster_min_samples, args.min_cluster_size)
        cluster_id[flagged_indices] = labels

        n_noise = int((labels < 0).sum())
        n_confirmed = int((labels >= 0).sum())
        clusters_summary = summarize_clusters(flagged_points, flagged_distances, labels)

        print(f"  Spatially confirmed (Step B/C): {n_confirmed} points in "
              f"{len(clusters_summary)} cluster(s)")
        print(f"  Rejected as spatial noise (isolated, no corroborating neighbors): "
              f"{n_noise} points")
        if not clusters_summary:
            print("  WARNING: no clusters survived clustering/--min-cluster-size. "
                  "Consider lowering --cluster-min-samples/--min-cluster-size or "
                  "raising --cluster-eps (dbscan) if real damage sites are being "
                  "rejected as noise.")
        for c in clusters_summary:
            centroid = ", ".join(f"{v:.3f}" for v in c["centroid"])
            print(f"    cluster {c['cluster_id']}: {c['point_count']} points, "
                  f"centroid=({centroid}), mean|d|={c['mean_magnitude']:.4f}, "
                  f"max|d|={c['max_magnitude']:.4f}")
    elif args.cluster and n_significant == 0:
        print("\nSkipping clustering: no points passed Step A.")

    # --- Write output cloud ------------------------------------------------
    if args.keep_all:
        extra_fields = [("classified", "i4")]
        if args.cluster:
            # "i4" (32-bit), not "i8" (64-bit): the PLY format has no
            # standard 64-bit integer type at all, so plyfile's own type
            # table (PlyElement.describe -> _lookup_type) has no entry for
            # "i8" and raises ValueError/KeyError the moment it tries to
            # describe this field - confirmed from a real run's traceback.
            # cluster_id only ever holds -1 (noise) or a small cluster
            # index (at most a few hundred, nowhere near int32's ~2.1
            # billion ceiling), so 32-bit loses nothing here.
            extra_fields.append(("cluster_id", "i4"))
        new_dtype = vertex.data.dtype.descr + extra_fields
        new_data = np.empty(vertex.data.shape, dtype=new_dtype)
        for name in vertex.data.dtype.names:
            new_data[name] = vertex.data[name]
        new_data["classified"] = np.asarray(significant, dtype=np.int32)
        if args.cluster:
            new_data["cluster_id"] = cluster_id.astype(np.int32)
        out_element = PlyElement.describe(new_data, "vertex")
        field_note = "'classified' and 'cluster_id' fields" if args.cluster else "'classified' field"
        print(f"\n  Keeping all {n_total} points, added {field_note}.")
        n_written = n_total
    else:
        if args.cluster:
            keep_mask = cluster_id >= 0
            note = "flagged AND spatially confirmed (Step A + Step B/C)"
        else:
            keep_mask = significant
            note = "flagged (Step A only)"
        filtered_data = vertex.data[keep_mask]
        if args.cluster:
            # Same "i4" not "i8" fix as the keep_all branch above.
            new_dtype = filtered_data.dtype.descr + [("cluster_id", "i4")]
            new_data = np.empty(filtered_data.shape, dtype=new_dtype)
            for name in filtered_data.dtype.names:
                new_data[name] = filtered_data[name]
            new_data["cluster_id"] = cluster_id[keep_mask].astype(np.int32)
            filtered_data = new_data
        out_element = PlyElement.describe(filtered_data, "vertex")
        n_written = int(keep_mask.sum())
        print(f"\n  Writing only the {n_written} points that are {note}.")

    PlyData([out_element], text=ply.text).write(args.output)
    print(f"Saved to: {args.output}")

    # --- Step D: write the per-cluster summary sidecar ----------------------
    if args.cluster:
        summary_path = Path(args.output).with_suffix(".clusters.json")
        summary = {
            "source": str(args.input),
            "output": str(args.output),
            "threshold": args.threshold,
            "cluster_method": args.cluster_method,
            "cluster_eps": args.cluster_eps if args.cluster_method == "dbscan" else None,
            "cluster_min_samples": args.cluster_min_samples,
            "min_cluster_size": args.min_cluster_size,
            "n_total": n_total,
            "n_flagged": n_significant,
            "n_noise": n_noise,
            "n_confirmed": n_confirmed,
            "clusters": clusters_summary,
        }
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"Cluster summary saved to: {summary_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
