#!/usr/bin/env python3
"""Classify signed changes and group damage sites."""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from plyfile import PlyData, PlyElement

from cloud_io import find_distance_field


def find_uncertainty_field(vertex, distance_field_name):
    """Finds CloudCompare's M3C2 uncertainty field for --use-uncertainty, the same way find_distance_field() finds the distance field: the on-disk name varies (e.g."""
    field_names = vertex.data.dtype.names or ()
    candidates = [
        n for n in field_names if n != distance_field_name and "uncertain" in n.lower()
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        with_distance = [n for n in candidates if "distance" in n.lower()]
        if with_distance:
            return with_distance[0]
    return candidates[0]


def cluster_flagged_points(points, method, eps, min_samples, min_cluster_size):
    """Clusters just the flagged (Step A) points by 3D position."""
    if method == "hdbscan":
        from sklearn.cluster import HDBSCAN

        clusterer = HDBSCAN(
            min_cluster_size=max(int(min_cluster_size), 2),
            min_samples=min_samples,
            copy=True,
        )
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


def summarize_clusters(
    flagged_points,
    flagged_distances,
    labels,
    cluster_type=None,
    id_offset=0,
    flagged_change=None,
):
    """Step D: turns per-point cluster labels into one aggregate record per surviving cluster - centroid, point count, bounding extent (max-min per axis), mean and max M3C2 magnitude."""
    clusters = []
    n_clusters = int(labels.max()) + 1 if len(labels) and labels.max() >= 0 else 0
    for local_id in range(n_clusters):
        mask = labels == local_id
        pts = flagged_points[mask]
        mags = np.abs(flagged_distances[mask])
        mins = pts.min(axis=0)
        maxs = pts.max(axis=0)
        record = {
            "cluster_id": local_id + id_offset,
            "point_count": int(mask.sum()),
            "centroid": pts.mean(axis=0).tolist(),
            "extent": (maxs - mins).tolist(),
            "mean_magnitude": float(mags.mean()),
            "max_magnitude": float(mags.max()),
        }
        if cluster_type is not None:
            record["type"] = cluster_type
        if flagged_change is not None:
            record["mean_change_inward"] = float(np.mean(flagged_change[mask]))
        clusters.append(record)
    return clusters


# ---------------------------------------------------------------------------
# --find-holes: missing points (holes) and the direction of change.
# The functions below are the same logic as find_missing_points.py, kept as
# a copy on purpose - standalone scripts in this project do not import from
# each other.
# ---------------------------------------------------------------------------

CHANGE_NONE, CHANGE_ADDED, CHANGE_REMOVED, CHANGE_HOLE, CHANGE_UNMATCHED = 0, 1, 2, 3, 4


def add_fields(data, columns):
    """Returns a copy of the structured array `data` with extra columns.
    columns: list of (name, dtype, values)."""
    new_data = np.empty(
        data.shape,
        dtype=data.dtype.descr + [(name, dtype) for name, dtype, _v in columns],
    )
    for name in data.dtype.names:
        new_data[name] = data[name]
    for name, _dtype, values in columns:
        new_data[name] = values
    return new_data


def _voxel_first_index(xyz, voxel):
    """Index of one point in each voxel (cells of `voxel` meters)."""
    cells = np.floor((xyz - xyz.min(axis=0)) / voxel).astype(np.int64)
    packed = (cells[:, 0] << 42) | (cells[:, 1] << 21) | cells[:, 2]
    _, first = np.unique(packed, return_index=True)
    return first


def orient_by_probing(
    xyz,
    normals,
    voxel=0.05,
    reach=None,
    start=0.15,
    sample_spacing=0.25,
    vote_radius=0.75,
    max_gap=0.6,
    min_align=0.8,
    support_radius=0.15,
    support_points=8,
):
    """Finds, for each point, which side of its surface is the inside of the room - without an inside point."""
    from scipy.spatial import cKDTree

    n_points = len(xyz)
    side = np.zeros(n_points, dtype=np.int8)
    valid = np.isfinite(xyz).all(axis=1) & np.isfinite(normals).all(axis=1)
    valid &= np.linalg.norm(normals, axis=1) > 0.5
    valid_idx = np.where(valid)[0]
    info = {"samples": 0, "samples_decided": 0, "points_resolved": 0}
    if len(valid_idx) < 10:
        return side, info
    v_xyz = xyz[valid_idx]
    v_nrm = normals[valid_idx] / np.linalg.norm(
        normals[valid_idx], axis=1, keepdims=True
    )

    # The cloud the lines are tested against: one point per voxel.
    dense = v_xyz[_voxel_first_index(v_xyz, voxel)]
    tree = cKDTree(dense)
    tol = 1.5 * voxel

    # Coarse sample points.
    s_idx = _voxel_first_index(v_xyz, sample_spacing)
    s_xyz, s_nrm = v_xyz[s_idx], v_nrm[s_idx]
    info["samples"] = int(len(s_idx))

    if reach is None:
        # The longest line that can stay inside the cloud: its diagonal.
        reach = min(
            float(np.linalg.norm(v_xyz.max(axis=0) - v_xyz.min(axis=0))) + 0.5, 100.0
        )

    # Only a run of two or more hits in a row counts. A wall gives such a run
    # (the line crosses a band of about 3 steps). A single stray point does not.
    hits = {}
    for direction in (1.0, -1.0):
        count = np.zeros(len(s_idx), dtype=np.int32)
        run = np.zeros(len(s_idx), dtype=np.int32)
        for t in np.arange(start, reach, voxel):
            q = s_xyz + direction * t * s_nrm
            dist, _ = tree.query(q, k=1, distance_upper_bound=tol, workers=-1)
            hit = np.isfinite(dist)
            if hit.any():
                # A surface has many points around the hit. A stray point has none.
                near = tree.query_ball_point(
                    q[hit], r=support_radius, return_length=True, workers=-1
                )
                hit[np.where(hit)[0][near < support_points]] = False
            run = np.where(hit, run + 1, 0)
            count += np.where(run == 2, 2, np.where(run > 2, 1, 0))
        hits[direction] = count
    more = np.maximum(hits[1.0], hits[-1.0])
    less = np.minimum(hits[1.0], hits[-1.0])
    decided = (more >= 2) & (more >= 2 * less)
    d = np.where(decided, np.where(hits[1.0] > hits[-1.0], 1, -1), 0).astype(np.int8)

    # Each sample votes with the samples on the same plane.
    pairs = cKDTree(s_xyz).query_pairs(vote_radius, output_type="ndarray")
    votes = d.astype(np.float64)  # a sample votes for itself
    if len(pairs):
        i, j = pairs[:, 0], pairs[:, 1]
        cos = np.einsum("ij,ij->i", s_nrm[i], s_nrm[j])
        off_ij = np.abs(np.einsum("ij,ij->i", s_xyz[j] - s_xyz[i], s_nrm[i]))
        off_ji = np.abs(np.einsum("ij,ij->i", s_xyz[i] - s_xyz[j], s_nrm[j]))
        same_plane = (np.abs(cos) > 0.9) & (off_ij < 0.05) & (off_ji < 0.05)
        i, j, cos = i[same_plane], j[same_plane], cos[same_plane]
        flip = np.where(cos >= 0, 1.0, -1.0)
        np.add.at(votes, i, d[j] * flip)  # j's side, in i's normal direction
        np.add.at(votes, j, d[i] * flip)
    final = np.sign(votes).astype(np.int8)
    ok = final != 0
    info["samples_decided"] = int(ok.sum())
    if not ok.any():
        return side, info

    # Every point takes the side of the nearest decided sample.
    ok_tree = cKDTree(s_xyz[ok])
    ok_nrm, ok_side = s_nrm[ok], final[ok]
    k = min(8, int(ok.sum()))
    resolved = np.zeros(len(valid_idx), dtype=np.int8)
    for a in range(0, len(valid_idx), 500000):
        b = min(a + 500000, len(valid_idx))
        dist, nbr = ok_tree.query(
            v_xyz[a:b], k=k, distance_upper_bound=max_gap, workers=-1
        )
        dist = dist.reshape(b - a, k)
        nbr = nbr.reshape(b - a, k)
        good = np.isfinite(dist)
        nbr_safe = np.where(good, nbr, 0)
        cos = np.einsum("ij,ikj->ik", v_nrm[a:b], ok_nrm[nbr_safe])
        good &= np.abs(cos) >= min_align
        first = good.argmax(axis=1)
        has = good.any(axis=1)
        rows = np.arange(b - a)
        pick = nbr_safe[rows, first]
        sign = np.where(cos[rows, first] >= 0, 1, -1)
        resolved[a:b] = np.where(has, ok_side[pick] * sign, 0)
    side[valid_idx] = resolved
    info["points_resolved"] = int((resolved != 0).sum())
    return side, info


def orient_change_inward(vertex, xyz, distances, interior_point=None, method="auto"):
    """Turns the M3C2 distance so that its sign has the same meaning on every surface: + means the surface moved INTO the room (material added), - means it moved away from the room (material removed)."""
    names = vertex.data.dtype.names or ()
    if not all(c in names for c in ("nx", "ny", "nz")):
        return None, None
    normals = np.column_stack(
        [np.asarray(vertex[c], dtype=np.float64) for c in ("nx", "ny", "nz")]
    )
    if interior_point is not None:
        interior = np.asarray(interior_point, dtype=np.float64)
        method_used = "interior-point"
    else:
        interior = xyz.mean(axis=0)
        method_used = "centroid"
    facing = np.einsum("ij,ij->i", normals, interior - xyz)
    orient = np.where(facing >= 0.0, 1.0, -1.0)
    info = {
        "method": method_used,
        "interior": interior,
        "n_probe": 0,
        "n_fallback": 0,
        "disagree_with_centroid": 0.0,
    }

    if interior_point is None and method == "auto":
        side, probe = orient_by_probing(xyz, normals)
        found = side != 0
        if found.any():
            info["method"] = "probe"
            info["n_probe"] = int(found.sum())
            info["n_fallback"] = int((~found).sum())
            info["disagree_with_centroid"] = float(
                (side[found] != np.where(orient[found] > 0, 1, -1)).mean()
            )
            orient = np.where(found, side.astype(np.float64), orient)
        info["probe"] = probe
    return distances * orient, info


def cluster_missing(points, eps, min_samples):
    """Groups the NaN points in 3D. Returns one label for each point. -1 is noise."""
    from sklearn.cluster import DBSCAN

    return DBSCAN(eps=eps, min_samples=min_samples).fit_predict(points)


def measure_cluster(points):
    """Size of a cluster along its own best-fit plane.
    Returns width (long side), height (short side), thickness (off the
    plane), area, perimeter, centre and the plane basis."""
    center = points.mean(axis=0)
    _, _, vt = np.linalg.svd(points - center, full_matrices=False)
    local = (points - center) @ vt.T
    extent = local.max(axis=0) - local.min(axis=0)
    area = float(extent[0] * extent[1])
    perimeter = float(2.0 * (extent[0] + extent[1]))
    try:
        from scipy.spatial import ConvexHull

        hull = ConvexHull(local[:, :2])
        area = float(hull.volume)  # in 2D, "volume" is the area
        perimeter = float(hull.area)  # in 2D, "area" is the perimeter
    except Exception:
        pass
    return (
        float(extent[0]),
        float(extent[1]),
        float(extent[2]),
        area,
        perimeter,
        center,
        vt,
    )


def ring_valid_fraction(cluster_points, tree, valid_mask, own_mask, ring):
    """Share of valid M3C2 values in a ring around the cluster.
    The ring holds all points within `ring` meters of the cluster,
    except the cluster itself."""
    from scipy.spatial import cKDTree

    radius = (
        float(np.linalg.norm(cluster_points.max(axis=0) - cluster_points.min(axis=0)))
        + ring
    )
    center = cluster_points.mean(axis=0)
    near = np.array(tree.query_ball_point(center, radius), dtype=np.int64)
    near = near[~own_mask[near]]
    if len(near) == 0:
        return 0.0, 0
    distance, _ = cKDTree(cluster_points).query(tree.data[near])
    near = near[distance <= ring]
    if len(near) == 0:
        return 0.0, 0
    return float(valid_mask[near].mean()), int(len(near))


def fill_fraction(cluster_points, tree, missing_mask, center, basis, thickness):
    """Share of NaN points among all points inside the outline of the
    cluster. A real hole is empty: the share is near 1. A patchy dropout
    area has valid points mixed in: the share is lower."""
    from scipy.spatial import Delaunay

    local = (cluster_points - center) @ basis.T
    radius = float(np.linalg.norm(local.max(axis=0) - local.min(axis=0))) / 2.0 + 0.01
    near = np.array(tree.query_ball_point(center, radius), dtype=np.int64)
    if len(near) == 0:
        return 1.0
    near_local = (tree.data[near] - center) @ basis.T
    band = max(0.05, 2.0 * thickness)
    near = near[np.abs(near_local[:, 2]) <= band]
    near_local = near_local[np.abs(near_local[:, 2]) <= band]
    try:
        inside = Delaunay(local[:, :2]).find_simplex(near_local[:, :2]) >= 0
    except Exception:
        return 1.0
    if inside.sum() == 0:
        return 1.0
    return float(missing_mask[near[inside]].mean())


def find_holes(
    xyz,
    valid,
    eps,
    min_samples,
    min_points,
    min_width,
    ring,
    min_ring_valid,
    min_fill,
    projection_radius=0.0,
    unmatched_min_fill=None,
):
    """Finds the holes: clusters of points that have no M3C2 distance (valid is False), kept only when they pass the four tests (see the module docstring)."""
    n_total = len(xyz)
    labels_out = np.full(n_total, -1, dtype=np.int64)
    missing = ~valid
    n_missing = int(missing.sum())
    accepted, rejected, unmatched_found = [], [], []
    clustered = 0
    if n_missing >= min_samples:
        from scipy.spatial import cKDTree

        index = np.where(missing)[0]
        labels = cluster_missing(xyz[index], eps, min_samples)
        tree = cKDTree(xyz)
        for label in sorted(set(labels.tolist()) - {-1}):
            members = index[labels == label]
            clustered += len(members)
            pts = xyz[members]
            width, height, thickness, area, perimeter, center, basis = measure_cluster(
                pts
            )
            own = np.zeros(n_total, dtype=bool)
            own[members] = True
            fraction, ring_count = ring_valid_fraction(pts, tree, valid, own, ring)
            fill = fill_fraction(pts, tree, missing, center, basis, thickness)
            record = {
                "point_count": int(len(members)),
                "centroid": pts.mean(axis=0).tolist(),
                "extent": (pts.max(axis=0) - pts.min(axis=0)).tolist(),
                "width": width,
                "height": height,
                "thickness": thickness,
                "area": area,
                "ring_valid_fraction": fraction,
                "fill_fraction": fill,
            }
            if projection_radius > 0:
                r = projection_radius
                record["corrected_width"] = width + 2.0 * r
                record["corrected_height"] = height + 2.0 * r
                record["corrected_area"] = area + perimeter * r + np.pi * r * r
            reasons = []
            if len(members) < min_points:
                reasons.append(f"fewer than {min_points} points")
            if height < min_width:
                reasons.append(f"short side below {min_width} m")
            if fraction < min_ring_valid:
                reasons.append(
                    f"ring valid share {fraction:.2f} below {min_ring_valid}"
                )
            if fill < min_fill:
                reasons.append(f"fill share {fill:.2f} below {min_fill}")
            only_fill = len(reasons) == 1 and reasons[0].startswith("fill share")
            if (
                unmatched_min_fill is not None
                and only_fill
                and fill >= unmatched_min_fill
            ):
                unmatched_found.append((members, record))
            elif reasons:
                record["rejected_because"] = reasons
                rejected.append(record)
            else:
                accepted.append((members, record))
    accepted.sort(key=lambda item: -item[1]["area"])
    holes = []
    for number, (members, record) in enumerate(accepted):
        labels_out[members] = number
        holes.append(record)
    n_isolated = n_missing - clustered
    if unmatched_min_fill is None:
        return labels_out, holes, rejected, n_isolated
    unmatched_found.sort(key=lambda item: -item[1]["area"])
    unmatched_labels = np.full(n_total, -1, dtype=np.int64)
    unmatched = []
    for number, (members, record) in enumerate(unmatched_found):
        unmatched_labels[members] = number
        unmatched.append(record)
    return labels_out, holes, rejected, n_isolated, unmatched_labels, unmatched


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--input", required=True, help="M3C2 diff result .ply")
    parser.add_argument("--output", required=True, help="Filtered/classified .ply")
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Absolute distance (same units as the point cloud, "
        "usually meters) above which a point counts as real "
        "change. Points below this are dropped as noise. "
        "Required unless --use-uncertainty is given - with "
        "--use-uncertainty, this becomes optional and, if given, "
        "acts as an extra floor on top of the per-point test "
        "(a point must then clear both).",
    )
    parser.add_argument(
        "--use-uncertainty",
        action="store_true",
        help="Use CloudCompare's own per-point M3C2 uncertainty field "
        "for Step A instead of (or alongside) --threshold: a point "
        "is flagged if |distance| exceeds ITS OWN local uncertainty "
        "value, rather than one fixed number applied everywhere. "
        "Confirmed against real project data: this field is "
        "exactly CloudCompare's LOD95 formula, 1.96 x (local "
        "roughness + registration error), and thresholding against "
        "it exactly reproduces CloudCompare's own "
        "'significant_change' field, point for point. A single "
        "fixed --threshold has to be set high enough to clear the "
        "worst local uncertainty anywhere in the cloud - in "
        "practice, close to whatever the registration error alone "
        "imposes as a floor (1.96 x registration error, even on a "
        "perfectly smooth, densely-sampled surface) - which can "
        "bury real change in the scan's better-registered, lower-"
        "noise regions. This adapts per point instead. If "
        "--threshold is also given, a point must clear BOTH tests "
        "(use this for an extra safety margin on top of the "
        "per-point floor). Requires an uncertainty field in "
        "--input - CloudCompare writes one automatically for any "
        "M3C2 run.",
    )
    parser.add_argument(
        "--keep-all",
        action="store_true",
        help="Don't drop below-threshold or below-cluster points - "
        "instead keep everything and add 'classified' (Step A: "
        "1=passed the magnitude threshold, 0=did not) and, if "
        "clustering is on, 'cluster_id' (Step B/C: -1=not part "
        "of a surviving cluster, 0..N-1=cluster membership) "
        "fields. Useful if you want to inspect either filter's "
        "effect in CloudCompare before committing.",
    )
    parser.add_argument(
        "--no-cluster",
        dest="cluster",
        action="store_false",
        help="Disable spatial clustering (on by default). Without "
        "this, Step A's magnitude threshold is the only filter "
        "applied, matching the tool's original behavior - no "
        "'cluster_id' field, no *.clusters.json sidecar file.",
    )
    parser.add_argument(
        "--cluster-method",
        choices=["dbscan", "hdbscan"],
        default="dbscan",
        help="[cluster] dbscan: fixed-radius density clustering, "
        "already validated on this sensor/environment for plane "
        "segmentation - needs one --cluster-eps to work "
        "everywhere in the flagged-point set. hdbscan: adapts to "
        "variable density automatically, worth trying if flagged-"
        "point density varies a lot by surface angle/distance "
        "from scanner. Default: dbscan",
    )
    parser.add_argument(
        "--cluster-eps",
        type=float,
        default=0.05,
        help="[cluster, dbscan only] Max gap (meters) between flagged "
        "points to still count as the same damage site. M3C2 core "
        "point spacing is usually finer than a plane-segmentation "
        "cloud's, so this defaults tighter than segment_planes.py's "
        "--cluster-eps (0.15) - check core point spacing "
        "(point_spacing.py) if sites are splitting or merging "
        "unexpectedly. Default: 0.05",
    )
    parser.add_argument(
        "--cluster-min-samples",
        type=int,
        default=4,
        help="[cluster] DBSCAN/HDBSCAN's density parameter: how many "
        "flagged neighbors (within --cluster-eps, for dbscan) a "
        "point needs to seed a cluster at all. Default: 4",
    )
    parser.add_argument(
        "--min-cluster-size",
        type=int,
        default=4,
        help="[cluster] A cluster's own tunable size floor, separate "
        "from --cluster-min-samples - e.g. 'require at least 4 "
        "flagged points total to count as a real site,' an "
        "explicit, inspectable rejection criterion on top of "
        "whatever --cluster-min-samples produced. Clusters "
        "smaller than this are folded into noise (cluster_id=-1) "
        "and dropped by Step C, same as any other noise point. "
        "Default: 4",
    )

    holes = parser.add_argument_group(
        "holes and direction of change",
        "Used only with --find-holes. See the module docstring.",
    )
    holes.add_argument(
        "--find-holes",
        action="store_true",
        help="Also find holes (points with no M3C2 distance, where the new "
        "scan has no points) and the direction of change (added or "
        "removed). Adds the output fields change_inward and "
        "change_type, and a 'type' for every cluster in the "
        ".clusters.json file. Needs clustering (not --no-cluster) and "
        "scipy. Default: off",
    )
    holes.add_argument(
        "--interior-point",
        type=float,
        nargs=3,
        metavar=("X", "Y", "Z"),
        default=None,
        help="A point inside the room, in the coordinates of the file. It "
        "sets which side of a wall is the inside. Default: none - the "
        "inside is found automatically (see --interior-method).",
    )
    holes.add_argument(
        "--interior-method",
        choices=["auto", "centroid"],
        default="auto",
        help="How to find the inside of the room when --interior-point is "
        "not given. auto: look along each normal - a line into the room "
        "hits the opposite wall, a line away from the room hits nothing "
        "(works for an L-shaped room); points with no clear answer use "
        "the centre of all points. centroid: the centre of all points "
        "for every point (correct only for a box-shaped room). "
        "Default: auto",
    )
    holes.add_argument(
        "--projection-radius",
        type=float,
        default=0.0,
        help="Radius (m) of the M3C2 projection cylinder: half of the "
        "Search scale in the M3C2 params file. M3C2 finds valid values "
        "up to this distance inside a hole, so the measured hole is "
        "smaller than the real hole. With this value the hole records "
        "also hold corrected sizes. Default: 0 (no correction)",
    )
    holes.add_argument(
        "--min-hole-points",
        type=int,
        default=100,
        help="Smallest group of missing points that counts as a hole. " "Default: 100",
    )
    holes.add_argument(
        "--min-hole-width",
        type=float,
        default=0.05,
        help="Smallest short side (m) of a hole. This removes thin strips "
        "along edges and corners. Default: 0.05",
    )
    holes.add_argument(
        "--hole-eps",
        type=float,
        default=0.05,
        help="Largest gap (m) between missing points in one hole. " "Default: 0.05",
    )
    holes.add_argument(
        "--hole-min-samples",
        type=int,
        default=4,
        help="Density setting for the hole clustering. Default: 4",
    )
    holes.add_argument(
        "--ring",
        type=float,
        default=0.10,
        help="Width (m) of the ring around a hole for the ring test. " "Default: 0.10",
    )
    holes.add_argument(
        "--min-ring-valid",
        type=float,
        default=0.6,
        help="Smallest share (0 to 1) of valid M3C2 values in the ring. A "
        "low value means a shadow, not a hole. Default: 0.6",
    )
    holes.add_argument(
        "--min-fill",
        type=float,
        default=0.8,
        help="Smallest share (0 to 1) of missing points inside the outline "
        "of a hole. A low value means a patchy dropout area, not a "
        "hole. Default: 0.8",
    )
    holes.add_argument(
        "--no-unmatched",
        dest="find_unmatched",
        action="store_false",
        help="Turn off the 'unmatched' sites. By default, a cluster of points "
        "without an M3C2 distance that fails ONLY the fill test (see "
        "--min-fill) is kept as a site of the type 'unmatched' (a patch "
        "with no partner in the other scan: a moved or absent object) "
        "and not rejected. With this option, it is rejected as before.",
    )
    holes.add_argument(
        "--min-unmatched-fill",
        type=float,
        default=0.05,
        help="Smallest fill share (0 to 1) of an 'unmatched' site. A cluster "
        "below this share is rejected. Default: 0.05",
    )
    holes.add_argument(
        "--hole-value",
        type=float,
        default=0.10,
        help="Value (m) that change_inward shows for the points of a hole. "
        "It is for display only (colour scales). Default: 0.10",
    )

    args = parser.parse_args()
    if args.threshold is None and not args.use_uncertainty:
        parser.error("--threshold is required unless --use-uncertainty is given.")
    if args.find_holes and not args.cluster:
        parser.error("--find-holes needs clustering. Do not use it with --no-cluster.")

    print(f"Reading: {args.input}")
    ply = PlyData.read(args.input)
    if "vertex" not in ply:
        print("ERROR: no vertex data found - is this really a point cloud PLY?")
        return 1
    vertex = ply["vertex"]

    field_name = find_distance_field(vertex)
    if field_name is None:
        print(
            f"ERROR: no M3C2/distance field found. Available fields: "
            f"{list(vertex.data.dtype.names or ())}"
        )
        print(
            "Make sure --input points at the actual M3C2 result file, not one "
            "of the duplicate input-cloud copies CloudCompare also saves."
        )
        return 1
    print(f"  Available fields: {list(vertex.data.dtype.names or ())}")
    print(f"  Selected field: '{field_name}'")

    distances = np.asarray(vertex[field_name], dtype=np.float64)
    finite_mask = np.isfinite(distances)
    n_total = len(distances)
    n_invalid = int((~finite_mask).sum())
    if n_invalid:
        print(
            f"  Note: {n_invalid} points had no valid M3C2 distance (outside "
            f"max distance / insufficient neighbors) and are treated as unchanged"
            + (
                " by Step A. --find-holes checks them below."
                if args.find_holes
                else "."
            )
        )

    finite_distances = distances[finite_mask]
    if len(finite_distances) and (finite_distances >= 0).all():
        print(
            "  WARNING: every value in the selected field is >= 0. A real M3C2 "
            "signed distance should straddle zero on unchanged surfaces - an "
            "all-positive field usually means this is actually an uncertainty "
            "or magnitude field, not the real distance field. Double check "
            "'Selected field' above against what you expect, and check the "
            "'Available fields' list for a better match if this looks wrong."
        )

    uncertainty_field_name = None
    uncertainty = None
    if args.use_uncertainty:
        uncertainty_field_name = find_uncertainty_field(vertex, field_name)
        if uncertainty_field_name is None:
            print(
                f"ERROR: --use-uncertainty given but no uncertainty field found. "
                f"Available fields: {list(vertex.data.dtype.names or ())}"
            )
            return 1
        print(f"  Uncertainty field: '{uncertainty_field_name}'")
        uncertainty = np.asarray(vertex[uncertainty_field_name], dtype=np.float64)
        finite_uncertainty = uncertainty[np.isfinite(uncertainty)]
        if len(finite_uncertainty) and (finite_uncertainty < 0).any():
            print(
                "  WARNING: the selected uncertainty field has negative values - a "
                "real uncertainty/magnitude field should never be negative. Double "
                "check 'Uncertainty field' above against what you expect."
            )

    # --- Step A: magnitude threshold --------------------------------------
    if args.use_uncertainty:
        both_finite = finite_mask & np.isfinite(uncertainty)
        significant = both_finite & (np.abs(distances) > uncertainty)
        if args.threshold is not None:
            significant = significant & (np.abs(distances) >= args.threshold)
            mode_desc = (
                f"per-point uncertainty ('{uncertainty_field_name}') "
                f"AND >= {args.threshold}"
            )
        else:
            mode_desc = f"per-point uncertainty ('{uncertainty_field_name}')"
    else:
        significant = finite_mask & (np.abs(distances) >= args.threshold)
        mode_desc = f"fixed threshold >= {args.threshold}"
    n_significant = int(significant.sum())
    pct = (n_significant / n_total * 100) if n_total else 0.0

    print(f"  Field used: '{field_name}'")
    print(f"  Step A mode: {mode_desc}")
    print(f"  Total points: {n_total}")
    print(f"  Flagged as changed (Step A): {n_significant} ({pct:.2f}%)")
    if n_significant:
        flagged_vals = distances[significant]
        print(
            f"  Flagged distance range: {flagged_vals.min():.4f} to {flagged_vals.max():.4f}"
        )
        print(f"  Flagged distance mean magnitude: {np.mean(np.abs(flagged_vals)):.4f}")
    else:
        if args.use_uncertainty:
            print(
                "  WARNING: no points passed. Either nothing changed significantly, "
                "or the combination of --use-uncertainty and --threshold is too "
                "strict for this scan."
            )
        else:
            print(
                "  WARNING: no points passed the threshold. Either nothing changed "
                "significantly, or --threshold is set too high for this scan."
            )

    xyz = np.column_stack(
        [np.asarray(vertex[c], dtype=np.float64) for c in ("x", "y", "z")]
    )

    # --- Direction of change (--find-holes): added or removed ------------
    change_inward = None
    interior_used = None
    orient_info = None
    direction = np.zeros(n_total, dtype=np.int8)  # 0 none, 1 added, 2 removed
    if args.find_holes:
        change_inward, orient_info = orient_change_inward(
            vertex, xyz, distances, args.interior_point, args.interior_method
        )
        interior_used = orient_info["interior"] if orient_info else None
        if change_inward is None:
            print(
                "\nNOTE: --find-holes: the file has no point normals (nx, ny, nz), so the "
                "direction of change (added or removed) cannot be found. The damage sites "
                "get the type 'changed', and the fields change_inward and change_type are "
                "not written. Holes are still found."
            )
        else:
            direction[significant & (change_inward > 0)] = CHANGE_ADDED
            direction[significant & ~(change_inward > 0)] = CHANGE_REMOVED
            if orient_info["method"] == "probe":
                print(
                    f"\nDirection of change: the inside of the room was found for "
                    f"{orient_info['n_probe']} points by looking along each normal "
                    f"(a line into the room hits the opposite wall)."
                )
                if orient_info["n_fallback"]:
                    print(
                        f"  {orient_info['n_fallback']} points have no clear answer. "
                        f"They use the centre of all points "
                        f"({', '.join(f'{v:.3f}' for v in interior_used)})."
                    )
                share = orient_info["disagree_with_centroid"]
                if share > 0.02:
                    print(
                        f"  The centre of all points would give the other side for "
                        f"{share * 100:.0f}% of the points. The room is probably not a "
                        f"simple box, and this is the reason for the automatic method."
                    )
            else:
                where = (
                    "--interior-point"
                    if args.interior_point is not None
                    else "the centre of all points"
                )
                print(
                    f"\nDirection of change: the inside of the room is {where} "
                    f"({', '.join(f'{v:.3f}' for v in interior_used)})."
                )
            print(
                f"  Flagged as added: {int((direction == CHANGE_ADDED).sum())}, "
                f"removed: {int((direction == CHANGE_REMOVED).sum())}"
            )

    # --- Step B/C: spatial clustering of the flagged subset --------------
    cluster_id = np.full(n_total, -1, dtype=np.int64)
    clusters_summary = []
    n_noise = 0
    n_confirmed = 0
    n_added_points = 0
    n_removed_points = 0

    if args.cluster and n_significant > 0:
        method_label = args.cluster_method
        print(f"\nClustering {n_significant} flagged points (method={method_label})...")
        if args.find_holes and change_inward is not None:
            # Added and removed points are clustered one kind at a time, so a
            # damage site never mixes the two.
            groups = [("added", CHANGE_ADDED), ("removed", CHANGE_REMOVED)]
        else:
            groups = [("changed" if args.find_holes else None, None)]

        id_offset = 0
        for type_name, kind in groups:
            member = (
                significant if kind is None else (significant & (direction == kind))
            )
            flagged_indices = np.where(member)[0]
            if not len(flagged_indices):
                continue
            flagged_points = xyz[flagged_indices]
            flagged_distances = distances[flagged_indices]
            flagged_change = (
                change_inward[flagged_indices]
                if (change_inward is not None and type_name)
                else None
            )

            labels = cluster_flagged_points(
                flagged_points,
                method_label,
                args.cluster_eps,
                args.cluster_min_samples,
                args.min_cluster_size,
            )
            cluster_id[flagged_indices] = np.where(labels >= 0, labels + id_offset, -1)

            n_noise += int((labels < 0).sum())
            group_confirmed = int((labels >= 0).sum())
            n_confirmed += group_confirmed
            if type_name == "added":
                n_added_points = group_confirmed
            elif type_name == "removed":
                n_removed_points = group_confirmed
            part = summarize_clusters(
                flagged_points,
                flagged_distances,
                labels,
                cluster_type=type_name,
                id_offset=id_offset,
                flagged_change=flagged_change,
            )
            clusters_summary += part
            id_offset += len(part)

        print(
            f"  Spatially confirmed (Step B/C): {n_confirmed} points in "
            f"{len(clusters_summary)} cluster(s)"
        )
        print(
            f"  Rejected as spatial noise (isolated, no corroborating neighbors): "
            f"{n_noise} points"
        )
        if not clusters_summary:
            print(
                "  WARNING: no clusters survived clustering/--min-cluster-size. "
                "Consider lowering --cluster-min-samples/--min-cluster-size or "
                "raising --cluster-eps (dbscan) if real damage sites are being "
                "rejected as noise."
            )
        for c in clusters_summary:
            centroid = ", ".join(f"{v:.3f}" for v in c["centroid"])
            kind_text = f" [{c['type']}]" if "type" in c else ""
            print(
                f"    cluster {c['cluster_id']}{kind_text}: {c['point_count']} points, "
                f"centroid=({centroid}), mean|d|={c['mean_magnitude']:.4f}, "
                f"max|d|={c['max_magnitude']:.4f}"
            )
    elif args.cluster and n_significant == 0:
        print("\nSkipping clustering: no points passed Step A.")

    # --- Holes (--find-holes): points without an M3C2 distance ------------
    hole_records = []
    hole_rejected = []
    unmatched_records = []
    n_hole_points = 0
    n_unmatched_points = 0
    hole_mask = np.zeros(n_total, dtype=bool)
    unmatched_mask = np.zeros(n_total, dtype=bool)
    if args.find_holes:
        print(f"\nLooking for holes: {n_invalid} points have no M3C2 distance...")
        try:
            found = find_holes(
                xyz,
                finite_mask,
                args.hole_eps,
                args.hole_min_samples,
                args.min_hole_points,
                args.min_hole_width,
                args.ring,
                args.min_ring_valid,
                args.min_fill,
                args.projection_radius,
                unmatched_min_fill=(
                    args.min_unmatched_fill if args.find_unmatched else None
                ),
            )
            if len(found) == 4:  # --no-unmatched: the old result
                hole_labels, holes, hole_rejected, n_isolated = found
                unmatched_labels = np.full(n_total, -1, dtype=np.int64)
                unmatched = []
            else:
                (
                    hole_labels,
                    holes,
                    hole_rejected,
                    n_isolated,
                    unmatched_labels,
                    unmatched,
                ) = found
        except ImportError as e:
            print(
                f"ERROR: --find-holes needs scipy and scikit-learn ({e}). "
                f"Run: pip install scipy scikit-learn"
            )
            return 1
        first_hole_id = len(clusters_summary)
        for number, record in enumerate(holes):
            hole_records.append(
                {"cluster_id": first_hole_id + number, "type": "hole", **record}
            )
        hole_mask = hole_labels >= 0
        cluster_id[hole_mask] = hole_labels[hole_mask] + first_hole_id
        n_hole_points = int(hole_mask.sum())
        clusters_summary += hole_records

        # Patches with no partner that fail only the fill test: moved or absent objects.
        first_unmatched_id = len(clusters_summary)
        for number, record in enumerate(unmatched):
            unmatched_records.append(
                {
                    "cluster_id": first_unmatched_id + number,
                    "type": "unmatched",
                    **record,
                }
            )
        unmatched_mask = unmatched_labels >= 0
        cluster_id[unmatched_mask] = (
            unmatched_labels[unmatched_mask] + first_unmatched_id
        )
        n_unmatched_points = int(unmatched_mask.sum())
        clusters_summary += unmatched_records

        print(f"  Holes accepted: {len(hole_records)} ({n_hole_points} points)")
        for record in hole_records:
            c = ", ".join(f"{v:.3f}" for v in record["centroid"])
            print(
                f"    cluster {record['cluster_id']} [hole]: {record['point_count']} points, "
                f"centroid=({c}), size {record['width']:.2f} x {record['height']:.2f} m, "
                f"area {record['area']:.3f} m2, ring valid "
                f"{record['ring_valid_fraction']:.2f}, fill {record['fill_fraction']:.2f}"
            )
            if "corrected_area" in record:
                print(
                    f"        corrected for the projection radius: "
                    f"{record['corrected_width']:.2f} x {record['corrected_height']:.2f} m, "
                    f"area {record['corrected_area']:.3f} m2"
                )
        if args.find_unmatched:
            print(
                f"  Unmatched sites (no partner in the other scan, but not empty "
                f"like a hole): {len(unmatched_records)} ({n_unmatched_points} points)"
            )
            for record in unmatched_records:
                c = ", ".join(f"{v:.3f}" for v in record["centroid"])
                print(
                    f"    cluster {record['cluster_id']} [unmatched]: "
                    f"{record['point_count']} points, centroid=({c}), size "
                    f"{record['width']:.2f} x {record['height']:.2f} m, fill "
                    f"{record['fill_fraction']:.2f}"
                )
        print(f"  Clusters rejected as not a hole: {len(hole_rejected)}")
        for record in hole_rejected:
            c = ", ".join(f"{v:.2f}" for v in record["centroid"])
            print(
                f"    rejected: {record['point_count']} points at ({c}): "
                f"{'; '.join(record['rejected_because'])}"
            )
        print(f"  Missing points in no cluster at all: {n_isolated}")

    # Fields that --find-holes adds to the output cloud.
    change_columns = []
    if args.find_holes and change_inward is not None:
        shown = change_inward.astype(np.float32)
        shown[hole_mask] = -abs(args.hole_value)
        kind_field = direction.astype(np.int32)
        kind_field[hole_mask] = CHANGE_HOLE
        kind_field[unmatched_mask] = CHANGE_UNMATCHED
        change_columns = [
            ("change_inward", "f4", shown),
            ("change_type", "i4", kind_field),
        ]

    # --- Write output cloud ------------------------------------------------
    if args.keep_all:
        columns = [("classified", "i4", np.asarray(significant, dtype=np.int32))]
        if args.cluster:
            # "i4" (32-bit), not "i8" (64-bit): the PLY format has no
            # standard 64-bit integer type at all, so plyfile's own type
            # table (PlyElement.describe -> _lookup_type) has no entry for
            # "i8" and raises ValueError/KeyError the moment it tries to
            # describe this field - confirmed from a real run's traceback.
            # cluster_id only ever holds -1 (noise) or a small cluster
            # index (at most a few hundred, nowhere near int32's ~2.1
            # billion ceiling), so 32-bit loses nothing here.
            columns.append(("cluster_id", "i4", cluster_id.astype(np.int32)))
        columns += change_columns
        new_data = add_fields(vertex.data, columns)
        out_element = PlyElement.describe(new_data, "vertex")
        added_names = ", ".join(f"'{name}'" for name, _t, _v in columns)
        print(f"\n  Keeping all {n_total} points, added the field(s) {added_names}.")
        n_written = n_total
    else:
        if args.cluster:
            keep_mask = cluster_id >= 0
            note = "flagged AND spatially confirmed (Step A + Step B/C)" + (
                ", plus the points of holes" if args.find_holes else ""
            )
        else:
            keep_mask = significant
            note = "flagged (Step A only)"
        filtered_data = vertex.data[keep_mask]
        if args.cluster:
            # Same "i4" not "i8" fix as the keep_all branch above.
            columns = [("cluster_id", "i4", cluster_id[keep_mask].astype(np.int32))]
            columns += [
                (name, t, values[keep_mask]) for name, t, values in change_columns
            ]
            filtered_data = add_fields(filtered_data, columns)
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
            "use_uncertainty": args.use_uncertainty,
            "uncertainty_field": uncertainty_field_name,
            "cluster_method": args.cluster_method,
            "cluster_eps": (
                args.cluster_eps if args.cluster_method == "dbscan" else None
            ),
            "cluster_min_samples": args.cluster_min_samples,
            "min_cluster_size": args.min_cluster_size,
            "n_total": n_total,
            "n_flagged": n_significant,
            "n_noise": n_noise,
            "n_confirmed": n_confirmed,
            "clusters": clusters_summary,
        }
        if args.find_holes:
            # n_confirmed, n_noise and n_flagged keep their meaning: measured
            # change only. The points of holes are counted on their own.
            summary.update(
                {
                    "find_holes": True,
                    "direction_found": change_inward is not None,
                    "interior_point": (
                        interior_used.tolist() if interior_used is not None else None
                    ),
                    "interior_method": (
                        orient_info["method"] if change_inward is not None else None
                    ),
                    "n_orient_fallback": (
                        orient_info["n_fallback"] if change_inward is not None else None
                    ),
                    "orient_disagree_with_centroid": (
                        round(orient_info["disagree_with_centroid"], 4)
                        if change_inward is not None
                        else None
                    ),
                    "n_added_points": n_added_points,
                    "n_removed_points": n_removed_points,
                    "n_holes": len(hole_records),
                    "n_hole_points": n_hole_points,
                    "find_unmatched": bool(args.find_unmatched),
                    "n_unmatched": len(unmatched_records),
                    "n_unmatched_points": n_unmatched_points,
                    "min_unmatched_fill": args.min_unmatched_fill,
                    "n_missing": n_invalid,
                    "projection_radius": args.projection_radius,
                    "hole_value": args.hole_value,
                    "hole_eps": args.hole_eps,
                    "hole_min_samples": args.hole_min_samples,
                    "min_hole_points": args.min_hole_points,
                    "min_hole_width": args.min_hole_width,
                    "hole_ring": args.ring,
                    "min_ring_valid": args.min_ring_valid,
                    "min_fill": args.min_fill,
                    "holes_rejected": hole_rejected,
                }
            )
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"Cluster summary saved to: {summary_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
