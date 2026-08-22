#!/usr/bin/env python3
"""
segment_planes.py
====================
Classifies a point cloud's major boundary planes - floor, ceiling, and
individual walls - by writing a single combined cloud (classified.ply)
where every point keeps its original position and gains a
'classification' field (0 = unclassified, other values map to a surface
name in manifest.json) plus an 'is_envelope' field (0/1, coarse
floor/ceiling/wall vs. everything-else grouping). One cloud, one file,
carries cleanly through the rest of the pipeline instead of juggling N
separate ones.

Also writes envelope.ply: just the envelope points (floor+ceiling+walls
combined, no interior/clutter) - meant as direct input to
surface_reconstruction.py for one unified shell reconstruction pass,
since walls/floor/ceiling genuinely touch at real edges/corners and can
be reconstructed as one connected surface rather than separate
disconnected mesh patches per plane.

Also splits the "unclassified" bucket into real interior clutter vs.
junk that sits outside the room altogether (on by default, see
--no-envelope-filter): the detected floor/ceiling/wall points already
describe the room's own footprint and height range, so any unclassified
point that falls outside BOTH of those (with some slack - see
--envelope-margin) is very unlikely to be real interior content, and
much more likely scan noise or a stray return from beyond the walls.
This adds a new 'outside_envelope' field (0/1) to classified.ply -
labeled, not removed, matching this script's existing "never silently
discard" approach (see the unclassified handling below): every point
stays in classified.ply either way, only the field value changes. Pass
--write-envelope-filtered if you also want a separate, additional .ply
with the outside points actually removed, for direct use downstream
without re-filtering by hand.

Each detected surface's own separate .ply file (floor.ply, wall_1.ply,
etc.) and unclassified.ply are OFF by default - see --write-separate-
surfaces. Turn that on if you want each surface as its own separate file,
for example to visually tune parameters per surface, or so each surface
becomes its own separate prim in Omniverse (in Omniverse or any USD/DCC
viewer, a separate file per surface means you can click a wall in the
Stage panel and hide it to see inside the compartment, instead of the
whole room being one solid blob). manifest.json always lists every
detected surface's name, point count, normal, and Z range either way -
only the "file" field per surface is null when that surface's own .ply
wasn't written.

Also filters each accepted plane down to its largest spatially-connected
cluster (on by default, see --no-cluster-filter): RANSAC's inlier test
only checks distance to the infinite plane equation, not the surface's
actual physical boundary, so stray/disconnected points that happen to
share the same plane get pulled into "unclassified" instead of polluting
the combined cloud's classification field.

Also merges split detections of the same physical plane (on by default,
see --no-merge-coplanar): confirmed on real data that a wall obstructed
mid-span by clutter can get RANSAC-detected as two separate, duplicate-
looking surfaces instead of one - and a ceiling split this way loses its
other half to a misleading "horizontal_surface_N" label instead of being
recognized as the same ceiling.

Reuses the same RANSAC plane-detection approach as level_cloud.py (run
that first for best results - classification here assumes the cloud is
already level, so "floor" = lowest near-horizontal plane, "ceiling" =
highest near-horizontal plane, by Z position).

Requires:
    pip install open3d numpy

Usage:
    python segment_planes.py --input leveled_cleaned.ply --output-dir segmented/
    python segment_planes.py --input leveled_cleaned.ply --output-dir segmented/ --write-separate-surfaces

STATUS: run against real data. The floor/ceiling/wall classification is a
straightforward height + orientation heuristic, not tuned - if a wall
gets misclassified as ceiling or vice versa, that's the first thing to
check against the manifest.json summary this produces.
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import open3d as o3d
from plyfile import PlyData, PlyElement


def filter_largest_cluster(pcd, eps, min_points):
    """
    RANSAC's inlier test only checks perpendicular distance to the
    (infinite) plane equation - it has no concept of the surface's actual
    physical boundary. A stray point anywhere in space that happens to
    sit within distance_threshold of that plane gets included, even if
    it's spatially disconnected from the real wall/floor's footprint.

    This clusters a plane's points by spatial proximity (DBSCAN) and
    keeps only the largest connected cluster - the real surface. Returns
    (keep_mask, discard_mask) as boolean arrays local to this plane's
    own point set, so the caller can split kept vs. discarded and route
    discarded points back into 'unclassified' rather than deleting them.
    """
    labels = np.array(pcd.cluster_dbscan(eps=eps, min_points=min_points, print_progress=False))
    if len(labels) == 0 or labels.max() < 0:
        # Nothing formed a real cluster (all noise, per DBSCAN) - keep
        # everything rather than discard the whole plane.
        keep_mask = np.ones(len(labels), dtype=bool)
        return keep_mask, ~keep_mask

    counts = np.bincount(labels[labels >= 0])
    largest_label = np.argmax(counts)
    keep_mask = labels == largest_label
    return keep_mask, ~keep_mask


def write_classified_cloud(output_path, points, classification, outside_envelope=None):
    """
    Writes points as a .ply with added integer fields:
      classification: 0 = unclassified, other values map to a surface
      name via the manifest's 'classification_ids' (PLY can't hold string
      labels per point cleanly)
      is_envelope: 0 = unclassified/interior/clutter, 1 = any detected
      surface (floor/ceiling/wall_N) - a coarse grouping for cases where
      you want to filter/toggle "everything enclosing the space" vs.
      "everything else" in one step, without dealing with individual
      wall numbers. Named "envelope" (the surfaces separating interior
      from exterior) deliberately, not "structural" - this has no
      concept of load-bearing structure like pillars/beams, which
      aren't flat planes and wouldn't be detected here anyway.
      outside_envelope: 0 = inside the room (a detected surface itself,
      or an unclassified point that sits within the derived footprint/
      height range - real interior clutter/damage), 1 = an unclassified
      point that sits outside the derived footprint/height range -
      likely scan noise/junk beyond the walls, not real interior
      content. Always 0 for a detected surface's own points (they
      define the envelope, so they cannot sit outside it). Defaults to
      all-zero when the caller doesn't pass one - e.g. when
      --no-envelope-filter was given, or no envelope was detected at
      all to derive a footprint from.

    Works on any subset of points (not just the full cloud), so the same
    function builds classified.ply, envelope.ply, and the optional
    envelope-filtered.ply - just pass a filtered points/classification/
    outside_envelope triple for each.

    Uses plyfile rather than open3d's writer, same as other scripts in
    this project - open3d's point cloud writer doesn't support arbitrary
    named scalar fields, only positions/normals/colors.
    """
    is_envelope = (classification > 0).astype(np.int32)
    if outside_envelope is None:
        outside_envelope = np.zeros(len(points), dtype=np.int32)
    vertex_dtype = [("x", "f4"), ("y", "f4"), ("z", "f4"),
                     ("classification", "i4"), ("is_envelope", "i4"),
                     ("outside_envelope", "i4")]
    vertex_data = np.zeros(len(points), dtype=vertex_dtype)
    vertex_data["x"] = points[:, 0].astype(np.float32)
    vertex_data["y"] = points[:, 1].astype(np.float32)
    vertex_data["z"] = points[:, 2].astype(np.float32)
    vertex_data["classification"] = classification.astype(np.int32)
    vertex_data["is_envelope"] = is_envelope
    vertex_data["outside_envelope"] = np.asarray(outside_envelope, dtype=np.int32)

    element = PlyElement.describe(vertex_data, "vertex")
    PlyData([element], text=False).write(str(output_path))


def convex_hull_2d(points_xy):
    """
    Andrew's monotone chain convex hull - a standard, compact O(n log n)
    algorithm, used here instead of adding a scipy dependency just for
    ConvexHull (this project keeps to numpy/open3d/plyfile wherever a
    hand-rolled version is this simple). Returns hull vertices in
    counter-clockwise order. Input is an (N, 2) array of XY coordinates;
    output is an (M, 2) array, M <= N. Returns fewer than 3 points when
    the input is degenerate (fewer than 3 unique points, or all
    collinear) - callers must check for this themselves, since a
    2-point-or-fewer "hull" cannot enclose any area.
    """
    pts = np.unique(points_xy, axis=0)
    if len(pts) < 3:
        return pts
    pts = pts[np.lexsort((pts[:, 1], pts[:, 0]))]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)

    upper = []
    for p in pts[::-1]:
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)

    hull = lower[:-1] + upper[:-1]
    return np.array(hull)


def points_inside_hull(points_xy, hull, margin=0.0):
    """
    Vectorized "inside a convex polygon, with an outward margin" test.

    hull: CCW-ordered vertices from convex_hull_2d(). For each polygon
    edge (A -> B), a query point's signed perpendicular distance to that
    edge's line is computed via the standard cross-product half-plane
    test (positive = left of the edge = inside, for a CCW polygon). A
    point counts as inside the (margin-expanded) polygon when every
    edge's signed distance is >= -margin - i.e. up to `margin` meters
    outside the exact hull boundary still counts as inside. This gives
    real walls some slack: a wall's own points don't sit exactly on a
    mathematical hull edge either, the same reason RANSAC's own
    distance_threshold and the Z-range margin below both carry slack.

    Not an exact geometric outward offset at the corners - each edge is
    offset independently, so the allowed region's corners end up
    slightly larger than a true rounded/mitered offset would give. Close
    enough for a coarse "roughly inside the room, or not" split; not a
    precision boundary.

    Returns an all-True array (nothing flagged) when the hull has fewer
    than 3 vertices (degenerate - see convex_hull_2d) - safer to skip
    filtering than to incorrectly flag real points as outside.
    """
    n_points = len(points_xy)
    if len(hull) < 3:
        return np.ones(n_points, dtype=bool)

    inside = np.ones(n_points, dtype=bool)
    n_edges = len(hull)
    for i in range(n_edges):
        a = hull[i]
        b = hull[(i + 1) % n_edges]
        edge = b - a
        edge_len = np.linalg.norm(edge)
        if edge_len < 1e-9:
            continue
        cross_z = edge[0] * (points_xy[:, 1] - a[1]) - edge[1] * (points_xy[:, 0] - a[0])
        signed_dist = cross_z / edge_len
        inside &= signed_dist >= -margin
    return inside


def load_cloud(path):
    pcd = o3d.io.read_point_cloud(str(path))
    if len(pcd.points) == 0:
        raise ValueError(f"'{path}' loaded but has zero points.")
    return pcd


def find_plane_candidates(pcd, max_planes=10, distance_threshold=0.02,
                           ransac_n=3, num_iterations=1000, min_inlier_fraction=0.015,
                           horizontal_threshold=0.7, max_horizontal_z_span=0.3,
                           max_attempts=None):
    """
    Same iterative RANSAC approach as level_cloud.py's version, but keeps
    more candidates by default (max_planes=10) since a room has several
    walls, not just one floor to find.

    Also rejects "fake" near-horizontal candidates that are actually
    diagonal slices cutting across multiple real surfaces at a shallow
    angle - a real RANSAC failure mode when distance_threshold is loose
    enough to let it "cheat" this way, confirmed on real data (a
    "horizontal" plane with Z span of 0.9m - a genuine floor/ceiling/table
    should only span its own thickness/noise, not the better part of a
    meter). Rejected candidates still have their points removed from
    further consideration (to make progress and avoid re-finding the same
    band again), but don't count against max_planes and aren't included
    in the output - their points end up in the "unclassified" bucket
    afterward. This only affects candidates classified as near-horizontal;
    wall detection (near-vertical normals) is untouched, including small
    real walls like an alcove/nook that might otherwise get starved of
    iteration budget by these artifacts.

    max_attempts: hard ceiling on total RANSAC calls (accepted + rejected
    combined), so a cloud that's mostly rejected bands can't loop forever
    without making progress toward max_planes. Defaults to 4x max_planes.
    """
    if max_attempts is None:
        max_attempts = max_planes * 4

    remaining_indices = np.arange(len(pcd.points))
    remaining = pcd
    total_points = len(pcd.points)
    candidates = []
    n_accepted = 0
    n_attempts = 0
    n_rejected = 0

    while n_accepted < max_planes and n_attempts < max_attempts:
        if len(remaining.points) < ransac_n * 2:
            break
        n_attempts += 1

        plane_model, inliers = remaining.segment_plane(
            distance_threshold, ransac_n, num_iterations)
        if len(inliers) < total_points * min_inlier_fraction:
            break  # too small to be useful at all - further attempts unlikely to help

        a, b, c, d = plane_model
        normal = np.array([a, b, c], dtype=np.float64)
        normal /= np.linalg.norm(normal)
        inlier_points_local = np.asarray(remaining.select_by_index(inliers).points)
        global_indices = remaining_indices[inliers]
        z_span = float(inlier_points_local[:, 2].max() - inlier_points_local[:, 2].min())

        is_near_horizontal = abs(normal[2]) >= horizontal_threshold
        is_fake_band = is_near_horizontal and z_span > max_horizontal_z_span

        if not is_fake_band:
            candidates.append({
                "normal": normal,
                "d": d,
                "count": len(inliers),
                "centroid": inlier_points_local.mean(axis=0),
                "z_min": float(inlier_points_local[:, 2].min()),
                "z_max": float(inlier_points_local[:, 2].max()),
                "global_indices": global_indices,
            })
            n_accepted += 1
        else:
            n_rejected += 1

        # Points removed either way - whether accepted or rejected, the
        # next attempt should look at different data, not re-find the
        # same band immediately.
        mask = np.ones(len(remaining.points), dtype=bool)
        mask[inliers] = False
        remaining_indices = remaining_indices[mask]
        remaining = remaining.select_by_index(inliers, invert=True)

    if n_rejected:
        print(f"  Rejected {n_rejected} near-horizontal candidate(s) as likely diagonal "
              f"artifacts (Z span over {max_horizontal_z_span}m) - their points were still "
              f"removed from consideration and will show up in 'unclassified' for review.")

    return candidates


def merge_coplanar_candidates(candidates, all_points, normal_cos_threshold=0.98,
                               plane_distance_tolerance=0.1):
    """
    Merges candidates that represent the SAME physical plane, detected
    twice across separate RANSAC iterations - a real failure mode, not
    hypothetical: confirmed on real data as a wall split into two
    non-overlapping detections (likely due to clutter/machinery
    obstructing the middle of the wall, leaving two disconnected point
    clusters that each independently satisfy the plane equation), which
    also explains a ceiling appearing to nearly vanish (classify_planes
    only keeps the single highest horizontal candidate as "ceiling" - its
    other half, split off as a separate candidate, ends up mislabeled as
    a horizontal_surface instead of being combined back in).

    Distinguishes "same plane, split in two" from "two genuinely parallel
    but physically separate surfaces" (e.g. opposite walls of a room,
    which share a normal direction but sit at very different distances)
    by checking BOTH: normal direction (parallel, sign-agnostic so a
    normal vs. its exact opposite still counts) AND plane offset (is
    each candidate's centroid actually close to the other's plane, not
    just similarly oriented).
    """
    def same_plane(a, b):
        cos_angle = abs(np.dot(a["normal"], b["normal"]))
        if cos_angle < normal_cos_threshold:
            return False
        dist_a_to_b_plane = abs(np.dot(b["normal"], a["centroid"]) + b["d"])
        dist_b_to_a_plane = abs(np.dot(a["normal"], b["centroid"]) + a["d"])
        return dist_a_to_b_plane < plane_distance_tolerance and \
            dist_b_to_a_plane < plane_distance_tolerance

    merged = []
    used = [False] * len(candidates)

    for i, cand in enumerate(candidates):
        if used[i]:
            continue
        group = [cand]
        used[i] = True
        for j in range(i + 1, len(candidates)):
            if not used[j] and same_plane(cand, candidates[j]):
                group.append(candidates[j])
                used[j] = True

        if len(group) == 1:
            merged.append(cand)
            continue

        largest = max(group, key=lambda c: c["count"])
        combined_indices = np.concatenate([c["global_indices"] for c in group])
        combined_points = all_points[combined_indices]
        merged.append({
            "normal": largest["normal"],
            "d": largest["d"],
            "count": len(combined_indices),
            "centroid": combined_points.mean(axis=0),
            "z_min": float(combined_points[:, 2].min()),
            "z_max": float(combined_points[:, 2].max()),
            "global_indices": combined_indices,
        })
        print(f"  Merged {len(group)} split detections of the same physical plane "
              f"(normal={np.round(largest['normal'], 3)}) into one, "
              f"{len(combined_indices)} total points.")

    return merged


def classify_planes(candidates, horizontal_threshold=0.7):
    """
    Labels each candidate as floor / ceiling / wall_N. Horizontal planes
    (|normal.z| above the threshold) are split into floor vs ceiling by
    which one sits lower - assumes the cloud is already roughly level
    (run level_cloud.py first). Everything else is a numbered wall,
    ordered largest-first.
    """
    horizontal = [c for c in candidates if abs(c["normal"][2]) >= horizontal_threshold]
    vertical = [c for c in candidates if abs(c["normal"][2]) < horizontal_threshold]

    labeled = []

    if horizontal:
        horizontal_sorted = sorted(horizontal, key=lambda c: c["centroid"][2])
        floor = horizontal_sorted[0]
        labeled.append(("floor", floor))
        if len(horizontal_sorted) > 1:
            ceiling = horizontal_sorted[-1]
            labeled.append(("ceiling", ceiling))
        # Anything horizontal in between (tables, shelves, machinery tops)
        for i, c in enumerate(horizontal_sorted[1:-1], start=1):
            labeled.append((f"horizontal_surface_{i}", c))

    vertical_sorted = sorted(vertical, key=lambda c: -c["count"])
    for i, c in enumerate(vertical_sorted, start=1):
        labeled.append((f"wall_{i}", c))

    return labeled


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, help="Point cloud .ply (ideally leveled first)")
    parser.add_argument("--output-dir", required=True, help="Folder to write segmented .ply files into")
    parser.add_argument("--distance-threshold", type=float, default=0.05,
                         help="RANSAC plane-fit tolerance. Default: 0.05")
    parser.add_argument("--max-planes", type=int, default=20,
                         help="Max number of ACCEPTED planes to find (rejected diagonal-"
                              "artifact candidates don't count against this). Default: 20")
    parser.add_argument("--max-attempts", type=int, default=None,
                         help="Hard ceiling on total RANSAC attempts (accepted + rejected). "
                              "Default: 4x --max-planes")
    parser.add_argument("--horizontal-threshold", type=float, default=0.7,
                         help="|normal.z| above this counts as floor/ceiling rather "
                              "than a wall - used both for classification and for the "
                              "diagonal-artifact rejection check. Default: 0.7")
    parser.add_argument("--max-horizontal-z-span", type=float, default=0.3,
                         help="A candidate classified as near-horizontal (see "
                              "--horizontal-threshold) but whose points span more Z than "
                              "this gets rejected as a likely diagonal artifact rather than "
                              "a real floor/ceiling/table. Default: 0.3 (meters)")
    parser.add_argument("--min-inlier-fraction", type=float, default=0.003,
                         help="A candidate plane must contain at least this fraction of "
                              "all points to be accepted at all (0.003 = 0.3%%). A smaller "
                              "real surface - like an alcove/nook wall - can be genuinely "
                              "smaller than a main wall and still fall under this bar; "
                              "raise it (e.g. 0.015) if too many small, spurious surfaces "
                              "are getting accepted instead of falling into 'unclassified'. "
                              "Default: 0.003")
    parser.add_argument("--no-cluster-filter", dest="cluster_filter", action="store_false",
                         help="Disable cluster-based stray-point removal (on by default). "
                              "RANSAC's inlier test only checks distance to the infinite "
                              "plane equation, not the surface's real physical boundary - "
                              "this keeps only the largest spatially-connected cluster per "
                              "plane, moving disconnected stray points to 'unclassified' "
                              "instead of leaving them mixed into that surface's points.")
    parser.add_argument("--cluster-eps", type=float, default=0.5,
                         help="[cluster filter] Max gap (meters) between points to still "
                              "count as the same connected surface - bridges small gaps "
                              "like window frames/shadows without merging genuinely "
                              "separate objects that happen to share a plane. Default: 0.5")
    parser.add_argument("--cluster-min-points", type=int, default=20,
                         help="[cluster filter] Minimum points to count as a real cluster "
                              "at all, in DBSCAN's own sense (not related to "
                              "--min-inlier-fraction). Default: 20")
    parser.add_argument("--no-merge-coplanar", dest="merge_coplanar", action="store_false",
                         help="Disable merging of split detections of the same physical "
                              "plane (on by default). Without this, a wall obstructed "
                              "mid-span by clutter can get detected as two separate, "
                              "duplicate-looking surfaces instead of one - and a ceiling "
                              "split this way loses its other half to a "
                              "'horizontal_surface_N' label instead of being combined in.")
    parser.add_argument("--merge-normal-cos", type=float, default=0.98,
                         help="[merge] How parallel two candidates' normals must be to be "
                              "considered the same plane (1.0 = exactly parallel). "
                              "Default: 0.98")
    parser.add_argument("--merge-distance", type=float, default=0.1,
                         help="[merge] How close (meters) a candidate's centroid must sit "
                              "to another candidate's plane to count as the same physical "
                              "surface, not just a parallel-but-separate one (e.g. opposite "
                              "walls of a room). Default: 0.1")
    parser.add_argument("--write-separate-surfaces", action="store_true",
                         help="Also write each detected surface's own .ply file "
                              "(floor.ply, wall_1.ply, etc.) and unclassified.ply, in "
                              "addition to the combined classified.ply. Off by default - "
                              "classified.ply (every point, with a 'classification' field) "
                              "already carries every point through the rest of the "
                              "pipeline as one cloud. Turn this on only if you want "
                              "per-surface files, e.g. to visually tune parameters per "
                              "surface, or so each surface becomes its own separate prim "
                              "in Omniverse.")
    parser.add_argument("--no-envelope-filter", dest="envelope_filter", action="store_false",
                         help="Disable splitting 'unclassified' into interior clutter vs. "
                              "outside-the-room junk (on by default). With this off, every "
                              "unclassified point's 'outside_envelope' field in "
                              "classified.ply is left at 0 (not evaluated), same as before "
                              "this feature existed.")
    parser.add_argument("--envelope-margin", type=float, default=0.15,
                         help="[envelope filter] Slack, in meters, given to the room's "
                              "derived footprint (horizontally) and height range "
                              "(vertically) before an unclassified point counts as "
                              "'outside_envelope'. A real wall's own points scatter a "
                              "little around the wall's true position, so some slack avoids "
                              "flagging real clutter sitting close to a wall/floor/ceiling "
                              "as junk. Too large a value can also let real outside junk "
                              "through unflagged - this is a coarse filter, not a precision "
                              "boundary. Default: 0.15")
    parser.add_argument("--write-envelope-filtered", action="store_true",
                         help="Also write an additional <name>_envelope_filtered.ply: every "
                              "point from classified.ply EXCEPT those flagged "
                              "'outside_envelope=1'. Off by default - classified.ply always "
                              "keeps every point either way (labeled, not removed, per this "
                              "script's usual approach); this flag only controls whether a "
                              "second, already-filtered copy also gets written for direct "
                              "downstream use. Has no effect if --no-envelope-filter is set, "
                              "or if no envelope was detected to filter against.")
    args = parser.parse_args()

    print(f"Loading: {args.input}")
    pcd = load_cloud(args.input)
    print(f"  {len(pcd.points)} points loaded.")
    all_points = np.asarray(pcd.points)

    print("Searching for candidate planes...")
    candidates = find_plane_candidates(
        pcd, max_planes=args.max_planes, distance_threshold=args.distance_threshold,
        horizontal_threshold=args.horizontal_threshold,
        max_horizontal_z_span=args.max_horizontal_z_span,
        max_attempts=args.max_attempts,
        min_inlier_fraction=args.min_inlier_fraction)

    if not candidates:
        print("ERROR: no planes found - the cloud may be too sparse or the "
              "distance threshold too tight.")
        return 1

    if args.merge_coplanar:
        print("Checking for split detections of the same physical plane...")
        candidates = merge_coplanar_candidates(
            candidates, all_points,
            normal_cos_threshold=args.merge_normal_cos,
            plane_distance_tolerance=args.merge_distance)

    labeled = classify_planes(candidates, args.horizontal_threshold)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Every .ply this run writes is prefixed with the output folder's own
    # name (in project mode, that's the numbered per-run folder
    # PROJECT_SCHEMA_v2.md Section 13.3 already creates, e.g.
    # "compartment_04_segment_001") instead of a fixed generic name like
    # "classified.ply". Two different runs' output folders never collide
    # (Section 13.1's own sequence numbering already guarantees that), but
    # the FILES inside used to all share the same bare names regardless -
    # confusing once a file is opened outside its folder (for example,
    # several runs' classified.ply all show up as just "classified.ply" in
    # CloudCompare's DB tree if opened together, with no way to tell which
    # is which at a glance). manifest.json keeps its fixed, unprefixed
    # name - resolve_segment_output() looks for it there specifically, and
    # it is a machine-read sidecar, not something opened directly in a
    # viewer, so the same collision risk does not apply to it.
    name_prefix = output_dir.name.strip() or "segment"

    manifest = {"source": str(args.input), "surfaces": []}
    all_classified_indices = []

    print(f"\nFound {len(labeled)} surfaces:")
    for name, plane in labeled:
        surface_points = all_points[plane["global_indices"]]
        surface_pcd = o3d.geometry.PointCloud()
        surface_pcd.points = o3d.utility.Vector3dVector(surface_points)

        if args.cluster_filter:
            keep_mask, discard_mask = filter_largest_cluster(
                surface_pcd, args.cluster_eps, args.cluster_min_points)
            n_stray = int(discard_mask.sum())
            if n_stray > 0:
                plane["global_indices"] = plane["global_indices"][keep_mask]
                surface_points = surface_points[keep_mask]
                surface_pcd.points = o3d.utility.Vector3dVector(surface_points)
                plane["count"] = len(surface_points)
                plane["z_min"] = float(surface_points[:, 2].min())
                plane["z_max"] = float(surface_points[:, 2].max())

        all_classified_indices.append(plane["global_indices"])

        if args.write_separate_surfaces:
            out_path = output_dir / f"{name_prefix}_{name}.ply"
            o3d.io.write_point_cloud(str(out_path), surface_pcd)
            destination_note = f"-> {out_path}"
            file_field = str(out_path)
        else:
            destination_note = "(not written - use --write-separate-surfaces to also save this file)"
            file_field = None

        stray_note = f" ({n_stray} stray points moved to unclassified)" if args.cluster_filter and n_stray > 0 else ""
        print(f"  {name}: {plane['count']} points, normal={np.round(plane['normal'], 3)}, "
              f"Z range=[{plane['z_min']:.3f}, {plane['z_max']:.3f}] {destination_note}{stray_note}")

        manifest["surfaces"].append({
            "name": name,
            "file": file_field,
            "point_count": plane["count"],
            "normal": plane["normal"].tolist(),
            "z_min": plane["z_min"],
            "z_max": plane["z_max"],
        })

    # Points that didn't match any detected plane are kept, not discarded -
    # counted (and, with --write-separate-surfaces, saved) as their own
    # "unclassified" bucket so nothing is silently lost. This is also the
    # place to look when a real wall goes "missing": if it didn't survive
    # RANSAC as its own plane (e.g. broken up by clutter/debris stuck to
    # it), its points usually show up here as a flat-ish cluster rather
    # than scattered randomly - worth checking visually (via
    # --write-separate-surfaces, or classified.ply's own 'classification'
    # field, which is 0 for these points either way) if a wall count
    # looks lower than expected.
    if all_classified_indices:
        classified_mask = np.zeros(len(all_points), dtype=bool)
        classified_mask[np.concatenate(all_classified_indices)] = True
    else:
        classified_mask = np.zeros(len(all_points), dtype=bool)
    unclassified_indices = np.where(~classified_mask)[0]
    n_unclassified = len(unclassified_indices)

    if n_unclassified > 0:
        if args.write_separate_surfaces:
            unclassified_points = all_points[unclassified_indices]
            unclassified_pcd = o3d.geometry.PointCloud()
            unclassified_pcd.points = o3d.utility.Vector3dVector(unclassified_points)
            unclassified_path = output_dir / f"{name_prefix}_unclassified.ply"
            o3d.io.write_point_cloud(str(unclassified_path), unclassified_pcd)
            unclassified_note = f"-> {unclassified_path}"
            unclassified_file_field = str(unclassified_path)
        else:
            unclassified_note = "(not written - use --write-separate-surfaces to also save this file)"
            unclassified_file_field = None

        print(f"\n  unclassified: {n_unclassified} points "
              f"({n_unclassified / len(all_points) * 100:.1f}%), didn't match any "
              f"detected plane (clutter, machinery, debris, or a wall RANSAC didn't "
              f"lock onto) {unclassified_note}")

        manifest["surfaces"].append({
            "name": "unclassified",
            "file": unclassified_file_field,
            "point_count": int(n_unclassified),
            "normal": None,
            "z_min": None,
            "z_max": None,
        })
    else:
        print("\n  Every point was assigned to a detected plane - no unclassified points.")

    # Combined single-file output: every point from the input, with added
    # 'classification' (per-surface) and 'is_envelope' (coarse group)
    # fields instead of being split across separate per-surface files -
    # this is the stage's primary, always-written output. The separate
    # per-surface files above are opt-in (--write-separate-surfaces),
    # additional to this, not a replacement for it.
    classification = np.zeros(len(all_points), dtype=np.int32)
    id_to_name = {0: "unclassified"}
    for idx, (name, plane) in enumerate(labeled, start=1):
        classification[plane["global_indices"]] = idx
        id_to_name[idx] = name

    # Envelope-only subset (everything that's floor/ceiling/wall_N,
    # excluding unclassified/interior/clutter) - "envelope" as in the
    # surfaces enclosing the space, not load-bearing structure. Computed
    # here, before classified.ply is written, because the outside-
    # envelope filter below needs this same mask to derive the room's
    # footprint/height range.
    envelope_mask = classification > 0
    n_envelope = int(envelope_mask.sum())

    # Splits "unclassified" into real interior clutter vs. junk that
    # sits outside the room altogether - see the module docstring and
    # write_classified_cloud()'s docstring for the full explanation.
    # Only evaluated when there's an actual envelope to derive a
    # footprint/height range from; a detected surface's own points are
    # always outside_envelope=0 (they define the envelope, so by
    # construction they can't sit outside it).
    outside_envelope = np.zeros(len(all_points), dtype=np.int32)
    hull_vertex_count = None
    n_outside = 0
    if args.envelope_filter and n_envelope >= 3:
        envelope_xy = all_points[envelope_mask][:, :2]
        hull = convex_hull_2d(envelope_xy)
        if len(hull) >= 3:
            hull_vertex_count = len(hull)
            envelope_z = all_points[envelope_mask][:, 2]
            z_min_env = float(envelope_z.min()) - args.envelope_margin
            z_max_env = float(envelope_z.max()) + args.envelope_margin

            inside_xy = points_inside_hull(all_points[:, :2], hull, margin=args.envelope_margin)
            inside_z = (all_points[:, 2] >= z_min_env) & (all_points[:, 2] <= z_max_env)
            outside_mask = ~(inside_xy & inside_z)
            outside_mask[envelope_mask] = False  # envelope points define the envelope
            outside_envelope = outside_mask.astype(np.int32)
            n_outside = int(outside_mask.sum())

            n_inside_unclassified = n_unclassified - n_outside
            print(f"\nEnvelope-based filter (margin={args.envelope_margin}m, "
                  f"{hull_vertex_count}-vertex footprint): {n_outside} of {n_unclassified} "
                  f"unclassified point(s) sit outside the room's derived footprint/height "
                  f"range - flagged 'outside_envelope=1' in classified.ply (kept, not "
                  f"removed; likely scan noise/junk beyond the walls). The remaining "
                  f"{n_inside_unclassified} sit inside - real interior clutter/damage, not junk.")
        else:
            print("\nEnvelope-based filter: skipped - the envelope points are too close to "
                  "collinear to derive a real room footprint from (need a proper 2D hull, "
                  "not just a line).")
    elif args.envelope_filter:
        print("\nEnvelope-based filter: skipped - not enough detected floor/ceiling/wall "
              "surface (need at least 3 envelope points) to derive a room footprint.")

    classified_path = output_dir / f"{name_prefix}_classified.ply"
    write_classified_cloud(classified_path, all_points, classification, outside_envelope)
    manifest["classification_ids"] = id_to_name
    manifest["classified_cloud_file"] = str(classified_path)
    manifest["envelope_filter_applied"] = bool(hull_vertex_count is not None)
    manifest["envelope_margin"] = args.envelope_margin
    manifest["n_outside_envelope"] = n_outside

    print(f"\nCombined classified cloud (all {len(all_points)} points, "
          f"'classification' + 'is_envelope' + 'outside_envelope' fields) -> {classified_path}")
    print("  ID mapping:")
    for cid, cname in id_to_name.items():
        print(f"    {cid} = {cname}")

    if n_envelope > 0:
        envelope_path = output_dir / f"{name_prefix}_envelope.ply"
        write_classified_cloud(
            envelope_path, all_points[envelope_mask], classification[envelope_mask])
        manifest["envelope_cloud_file"] = str(envelope_path)
        print(f"\nEnvelope-only cloud ({n_envelope} points - floor/ceiling/walls "
              f"combined, excludes unclassified/interior) -> {envelope_path}")
        print("  Feed this into surface_reconstruction.py for one unified shell "
              "reconstruction instead of separate meshes per surface.")

    manifest["envelope_filtered_cloud_file"] = None
    if args.write_envelope_filtered:
        if n_outside > 0:
            keep_mask = outside_envelope == 0
            filtered_path = output_dir / f"{name_prefix}_envelope_filtered.ply"
            write_classified_cloud(
                filtered_path, all_points[keep_mask], classification[keep_mask],
                outside_envelope[keep_mask])
            manifest["envelope_filtered_cloud_file"] = str(filtered_path)
            print(f"\nEnvelope-filtered cloud ({int(keep_mask.sum())} points - "
                  f"classified.ply with the {n_outside} outside-envelope point(s) removed) "
                  f"-> {filtered_path}")
        else:
            print("\nEnvelope-filtered cloud: not written - no points were flagged "
                  "'outside_envelope', so it would be identical to classified.ply.")

    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nManifest saved to: {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
