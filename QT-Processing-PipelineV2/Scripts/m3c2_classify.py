#!/usr/bin/env python3
"""
m3c2_classify.py
==================
Takes the raw M3C2 diff result (a point per core point, each with a signed
distance value) and reduces it to just the points that represent real
change - filtering out the noise floor so the change-highlight cloud fed
into Stage 6 (Export) actually shows damage/debris, not the whole surface.

Two filtering passes, run in order:

  Step A (magnitude): a point must clear a significance test to be
  flagged at all. Two modes:
    - --threshold (default, original behavior, unchanged): a single
      fixed distance, applied everywhere.
    - --use-uncertainty: per-point, using CloudCompare's own M3C2
      uncertainty field instead of one fixed number - confirmed against
      real project data to be exactly CloudCompare's LOD95 formula,
      1.96 x (local roughness + registration error), and to reproduce
      its own 'significant_change' field point-for-point. A single fixed
      --threshold has to clear the worst local uncertainty anywhere in
      the cloud (in practice, close to whatever the registration error
      alone imposes as a floor - a real 20cm test object came out right
      on that boundary, fragmented, in the case this was built for), which
      can bury real change in better-registered, lower-noise regions of
      the same scan. See --use-uncertainty's own --help text for the
      full reasoning. The two modes can be combined (a point must then
      clear both).

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

Optional --find-holes step (off by default; the output is then exactly as
before). It adds two things:

  1. Holes. M3C2 gives NaN (no value) where the new scan has no points at
     all, for example a hole that opens to empty space. Steps A to D skip
     such points on purpose. --find-holes groups them and measures each
     group. A group is a hole when it passes four tests: enough points
     (--min-hole-points), a wide enough short side (--min-hole-width), a
     ring of valid M3C2 values around it (--min-ring-valid, this removes
     shadows) and an empty inside (--min-fill, this removes patchy
     dropout). The hole points get a cluster_id, so every later stage
     treats a hole as one more damage site.

     A group that fails ONLY the fill test is not thrown away. It is a
     patch with no partner in the other scan, but with valid points mixed
     in. A moved object looks like this: its faces have no partner, but
     its other faces and the points around it have a distance. Such a
     group is kept as a site of the type "unmatched" (a moved or absent
     object), if its fill share is at least --min-unmatched-fill (default
     0.05). --no-unmatched turns this off.

  2. Direction. CloudCompare turns all point normals to one fixed
     direction, so the sign of the M3C2 distance changes from one wall to
     the opposite wall. This step turns each normal to the inside of the
     room. It finds the inside automatically: a line along a normal into
     the room hits the opposite wall, and a line away from the room hits
     nothing (this also works in an L-shaped room). --interior-point gives
     the inside by hand. The new field change_inward is then + where material was ADDED (the surface moved
     into the room) and - where material was REMOVED. The flagged points
     are clustered separately for added and removed, so each damage site
     has one clear type. A hole counts as removed material.

     Every cluster in the .clusters.json file gets a "type": "added",
     "removed", "hole" or "unmatched". Hole and unmatched clusters also hold width, height,
     thickness, area (m2) and, with --projection-radius, corrected sizes.
     The output cloud gets the fields change_inward (m, a hole shows
     -hole-value) and change_type (0 none, 1 added, 2 removed, 3 hole,
     4 unmatched). The points of an unmatched site have no change_inward
     value (NaN), because they have no distance.

Requires:
    pip install plyfile numpy scikit-learn scipy     (scipy: --find-holes only)

Usage:
    python m3c2_classify.py --input diff_result.ply --output classified.ply --threshold 0.02
    python m3c2_classify.py --input diff_result.ply --output classified.ply --threshold 0.02 \\
        --cluster-method hdbscan --min-cluster-size 6
    python m3c2_classify.py --input diff_result.ply --output classified.ply --use-uncertainty
    python m3c2_classify.py --input diff_result.ply --output classified.ply --use-uncertainty \\
        --threshold 0.02
    python m3c2_classify.py --input diff_result.ply --output classified.ply --use-uncertainty \\
        --find-holes --projection-radius 0.25
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
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


def find_uncertainty_field(vertex, distance_field_name):
    """
    Finds CloudCompare's M3C2 uncertainty field for --use-uncertainty,
    the same way find_distance_field() finds the distance field: the
    on-disk name varies (e.g. 'distance_uncertainty'), so this matches
    on a normalized substring rather than requiring one exact spelling.
    Excludes distance_field_name itself so the two can never resolve to
    the same field. If more than one candidate remains (uncommon), the
    one that also mentions 'distance' is preferred, matching
    CloudCompare's own naming convention for this field; otherwise the
    first candidate is returned rather than raising, since --use-
    uncertainty's own caller already prints 'Uncertainty field: ...' so
    a wrong guess is visible immediately, not silent.
    """
    field_names = vertex.data.dtype.names or ()
    candidates = [n for n in field_names
                  if n != distance_field_name and "uncertain" in n.lower()]
    if not candidates:
        return None
    if len(candidates) > 1:
        with_distance = [n for n in candidates if "distance" in n.lower()]
        if with_distance:
            return with_distance[0]
    return candidates[0]


def estimate_background(xyz, normals, distances, radius, min_neighbors=5, min_dot=0.9):
    """
    The smooth part of the M3C2 distances: for every core point, the median of
    the distances of the core points within `radius` (metres) that lie on the
    same surface (the normals differ by less than about 25 degrees).

    Why: two scans of the same room never fit perfectly. The SLAM error of
    each scan is a smooth offset that changes slowly over the surface (a band
    some tens of centimetres wide). It is as large as a small damage (5 cm),
    and it is different in each pair of scans. Real damage has sharp edges. A
    median over a wide area follows the smooth offset and ignores a damage
    that covers less than half of the area. Distances minus this background
    show the damage.

    Limits:
      - A damage that covers more than half of the circle of `radius` is part
        of the background and is removed. Use a radius that is larger than the
        largest damage that you expect (the default of 0.5 m is for damage up
        to about 0.6 m wide).
      - A core point with fewer than `min_neighbors` neighbours on the same
        surface, or with no normal, gets a background of 0 (no change).

    How: the median is computed for one anchor point in every cell of
    radius / 4, and each core point takes the value of the nearest anchor on
    the same surface. This is much faster than one median per core point and
    the difference is below 1 mm. The sign of the distance depends on the
    direction of the normal, so a neighbour with the opposite normal counts
    with the opposite sign.

    Returns an array with the background (metres), 0 where there is none.
    """
    from scipy.spatial import cKDTree

    n_points = len(xyz)
    background = np.zeros(n_points, dtype=np.float64)
    nrm_len = np.linalg.norm(normals, axis=1)
    valid = np.isfinite(distances) & np.isfinite(xyz).all(axis=1) \
        & np.isfinite(normals).all(axis=1) & (nrm_len > 0.5)
    index = np.where(valid)[0]
    if len(index) < min_neighbors * 2 or radius <= 0:
        return background
    pts = xyz[index]
    nrm = normals[index] / nrm_len[index][:, None]
    dist = distances[index]
    tree = cKDTree(pts)

    spacing = radius / 4.0
    cell = np.floor((pts - pts.min(axis=0)) / spacing).astype(np.int64)
    packed = (cell[:, 0] << 42) | (cell[:, 1] << 21) | cell[:, 2]
    _, anchor = np.unique(packed, return_index=True)
    anchor_value = np.zeros(len(anchor), dtype=np.float64)
    anchor_ok = np.zeros(len(anchor), dtype=bool)
    for a in range(0, len(anchor), 500):
        part = anchor[a:a + 500]
        neighbours = tree.query_ball_point(pts[part], radius)
        for k, ids in enumerate(neighbours):
            ids = np.asarray(ids, dtype=np.int64)
            dot = nrm[ids] @ nrm[part[k]]
            same = np.abs(dot) >= min_dot
            if same.sum() >= min_neighbors:
                anchor_value[a + k] = np.median(dist[ids[same]] * np.sign(dot[same]))
                anchor_ok[a + k] = True

    # Every core point takes the value of the nearest anchor that has a value
    # and lies on the same surface.
    anchor_tree = cKDTree(pts[anchor])
    anchor_nrm = nrm[anchor]
    k = min(12, len(anchor))
    reach = 2.5 * spacing
    result = np.zeros(len(index), dtype=np.float64)
    for a in range(0, len(index), 200000):
        b = min(a + 200000, len(index))
        d_anchor, nb = anchor_tree.query(pts[a:b], k=k, distance_upper_bound=reach)
        d_anchor = d_anchor.reshape(b - a, k)
        nb = nb.reshape(b - a, k)
        good = np.isfinite(d_anchor)
        safe = np.where(good, nb, 0)
        dot = np.einsum("ij,ikj->ik", nrm[a:b], anchor_nrm[safe])
        good &= (np.abs(dot) >= min_dot) & anchor_ok[safe]
        first = good.argmax(axis=1)
        has = good.any(axis=1)
        rows = np.arange(b - a)
        pick = safe[rows, first]
        sign = np.where(dot[rows, first] >= 0, 1.0, -1.0)
        result[a:b] = np.where(has, anchor_value[pick] * sign, 0.0)
    background[index] = result
    return background


def find_density_fields(field_names):
    """
    Finds the two point-count fields that CloudCompare writes when the M3C2
    parameter ExportDensityAtProjScale is true: the number of points of cloud 1
    and of cloud 2 inside the projection cylinder of each core point. Returns
    (name_of_cloud1, name_of_cloud2), or None when the file has no such pair.

    The on-disk names vary (for example 'Npoints_cloud1' or 'density [cloud1]',
    with or without the prefix 'scalar_'), so the match is on a normalised name:
    it must have a word for a count (npoints, density, count) and end with, or
    contain, 'cloud1' / 'c1' (or the 2 forms). If the match is not exactly one
    field for each cloud, the result is None. Use --density-fields to give the
    names then.
    """
    def normal(name):
        name = name.lower()
        if name.startswith("scalar_"):
            name = name[len("scalar_"):]
        return re.sub(r"[^a-z0-9]", "", name)

    count_words = ("npoints", "numpoints", "density", "pointcount", "count")
    first, second = [], []
    for name in field_names:
        n = normal(name)
        if not any(w in n for w in count_words):
            continue
        if "cloud1" in n or "c1" in n or n.endswith("1"):
            first.append(name)
        elif "cloud2" in n or "c2" in n or n.endswith("2"):
            second.append(name)
    if len(first) == 1 and len(second) == 1:
        return first[0], second[0]
    return None


def density_drop_mask(n1, n2, ratio, min_base_points=20):
    """
    The core points where cloud 2 has much fewer points than cloud 1.

    n1, n2: the point counts in the projection cylinder (cloud 1 = the cloud
    with the core points, cloud 2 = the other cloud). The two scans can have a
    different density everywhere, so the ratio n2 / n1 is divided by its
    median (over all core points with at least min_base_points of cloud 1)
    before the test. A point is low when this relative ratio is below `ratio`.

    Why: where a hole opens in the second scan, M3C2 still finds a distance
    for a core point when its cylinder reaches the points around the hole. The
    distance is then not reliable, but the count of cloud 2 drops. The drop
    shows the hole even when the distance has a value.

    Returns (low, median_ratio, n_tested): low is a boolean array, median_ratio
    is the median of n2 / n1 (None when there is no data), n_tested is the
    number of core points that were tested.
    """
    n1 = np.asarray(n1, dtype=np.float64)
    n2 = np.asarray(n2, dtype=np.float64)
    tested = np.isfinite(n1) & np.isfinite(n2) & (n1 >= min_base_points)
    low = np.zeros(len(n1), dtype=bool)
    if tested.sum() < 100:
        return low, None, int(tested.sum())
    relative = np.zeros(len(n1), dtype=np.float64)
    relative[tested] = n2[tested] / n1[tested]
    median = float(np.median(relative[tested]))
    if median <= 0:
        return low, None, int(tested.sum())
    low[tested] = (relative[tested] / median) < ratio
    return low, median, int(tested.sum())


def persistence_filter(xyz, cluster_id, clusters, other_points, distance, share):
    """
    Keeps the damage sites that a second M3C2 run (another scale) also finds.

    Why: a site that comes from the scale of the first run only (for example a
    band of smooth offset between two scans, or noise on a curved surface) changes
    when the scale changes. Real damage is there at both scales.

    xyz: the core points of the first run. cluster_id: one integer for each core
    point (-1 = no site). clusters: the site records of the first run (each has
    "cluster_id"). other_points: the core points of the second run that belong to
    a site there (n x 3). distance: how near (metres) a point of the second run
    must be to a point of the site. share: the share (0 to 1) of the points of a
    site that need a point of the second run within `distance`.

    Returns (new_cluster_id, new_clusters, rejected, n_rejected_points):
      - new_cluster_id: the ids of the kept sites are 0, 1, 2, ... in the old order;
        the points of a rejected site get -1.
      - new_clusters: the kept records (a copy, with the new "cluster_id").
      - rejected: one record for each rejected site: the old id, "centroid",
        "point_count" and "persist_share" (the share that was found).
      - n_rejected_points: the number of points of the rejected sites.
    A site with no point at all counts as not found.
    """
    from scipy.spatial import cKDTree

    new_cluster_id = np.full(len(cluster_id), -1, dtype=np.int64)
    kept, rejected = [], []
    n_rejected_points = 0
    tree = cKDTree(np.asarray(other_points, dtype=np.float64)) if len(other_points) else None
    for record in clusters:
        members = np.where(cluster_id == record["cluster_id"])[0]
        found = 0.0
        if tree is not None and len(members):
            nearest, _ = tree.query(xyz[members])
            found = float(np.mean(nearest <= distance))
        if found >= share and len(members):
            new_id = len(kept)
            new_cluster_id[members] = new_id
            kept.append({**record, "cluster_id": new_id, "persist_share": round(found, 3)})
        else:
            n_rejected_points += len(members)
            rejected.append({"cluster_id": record["cluster_id"],
                             "centroid": record["centroid"],
                             "point_count": record["point_count"],
                             "persist_share": round(found, 3)})
    return new_cluster_id, kept, rejected, n_rejected_points


def site_fill_fraction(local_xy):
    """Share of the cells inside the outline of a site that hold at least one
    point of the site. local_xy: (n, 2) positions in the plane of the site. The
    cell size is 2 times the median distance to the nearest neighbour, so the
    number does not depend on the point spacing. The outline is the convex hull,
    so the number does not depend on how the site is turned in its plane. A
    compact patch gives a number near 1 (a patch can be lower where the scan
    has gaps). A ring-like or patchy false site gives a low number. A line gives
    a high number: the short-side limit rejects a line."""
    from scipy.spatial import cKDTree, ConvexHull
    n = len(local_xy)
    if n < 4:
        return 1.0
    spacing = float(np.median(cKDTree(local_xy).query(local_xy, k=2)[0][:, 1]))
    cell = max(2.0 * spacing, 1e-6)
    index = np.floor((local_xy - local_xy.min(axis=0)) / cell).astype(np.int64)
    occupied = len(np.unique(index[:, 0] * (index[:, 1].max() + 1) + index[:, 1]))
    try:
        hull_area = float(ConvexHull(local_xy).volume)       # in 2D, .volume is the area
    except Exception:                                         # all points on a line
        return 1.0
    total = max(hull_area / (cell * cell), float(occupied))
    return min(1.0, float(occupied) / total)


def narrowest_width(local_xy):
    """Width of the narrowest strip that holds all points of a site (metres).
    local_xy: (n, 2) positions in the plane of the site. The value does not depend
    on how the site is turned in its plane: a square of side L gives L (the long
    and short axes of a square are not defined, so an axis-based width would give
    up to 1.4 L). A line gives its thickness."""
    from scipy.spatial import ConvexHull
    if len(local_xy) < 3:
        return 0.0
    try:
        hull = local_xy[ConvexHull(local_xy).vertices]
    except Exception:                                         # all points on a line
        return 0.0
    edges = np.roll(hull, -1, axis=0) - hull
    length = np.linalg.norm(edges, axis=1)
    keep = length > 0
    edges, length = edges[keep], length[keep]
    normals = np.column_stack([-edges[:, 1], edges[:, 0]]) / length[:, None]
    spread = hull @ normals.T                                 # (vertices, edges)
    return float((spread.max(axis=0) - spread.min(axis=0)).min())


def site_size_filter(xyz, cluster_id, clusters, min_width, min_area, min_fill):
    """
    Keeps the damage sites that are large enough and well filled.

    Why: the false sites of a SLAM map are mostly small (a few centimetres wide)
    or thin (a line along an edge or a band of smooth offset). A real damage that
    is worth a report has a size. A limit in metres does not depend on the point
    spacing, and a limit on the share of filled cells does not either.

    xyz: the core points. cluster_id: one integer for each core point (-1 = no
    site). clusters: the site records (each has "cluster_id"). Only the measured
    sites are tested: a record with no "type", or with the type "added", "removed"
    or "changed". Holes and unmatched sites have their own limits and are kept.
    min_width: the short side of the site, in the plane of the site, in metres.
    min_area: the area of the outline of the site, in square metres.
    min_fill: the share of filled cells (see site_fill_fraction). A value of 0
    switches a limit off.

    Returns (new_cluster_id, new_clusters, rejected, n_rejected_points). The ids
    of the kept sites are 0, 1, 2, ... in the old order. The kept records get the
    keys "width", "height", "area" and "fill" of the site. A rejected record has
    the old id, "centroid", "point_count", the same four numbers and "reasons".
    """
    new_cluster_id = np.full(len(cluster_id), -1, dtype=np.int64)
    kept, rejected = [], []
    n_rejected_points = 0
    for record in clusters:
        members = np.where(cluster_id == record["cluster_id"])[0]
        if record.get("type", "changed") not in ("added", "removed", "changed") \
                or not len(members):
            new_id = len(kept)
            new_cluster_id[members] = new_id
            kept.append({**record, "cluster_id": new_id})
            continue
        width, height, _t, area, _p, center, basis = measure_cluster(xyz[members])
        local = ((xyz[members] - center) @ basis.T)[:, :2]
        fill = site_fill_fraction(local)
        short_side = narrowest_width(local)
        reasons = []
        if min_width > 0 and short_side < min_width:
            reasons.append(f"short side {short_side * 100:.1f} cm < {min_width * 100:.1f} cm")
        if min_area > 0 and area < min_area:
            reasons.append(f"area {area * 1e4:.0f} cm2 < {min_area * 1e4:.0f} cm2")
        if min_fill > 0 and fill < min_fill:
            reasons.append(f"fill {fill:.2f} < {min_fill:.2f}")
        numbers = {"width": round(width, 4), "height": round(height, 4),
                   "short_side": round(short_side, 4), "area": round(area, 5), "fill": round(fill, 3)}
        if reasons:
            n_rejected_points += len(members)
            rejected.append({"cluster_id": record["cluster_id"],
                             "centroid": record["centroid"],
                             "point_count": record["point_count"], **numbers,
                             "reasons": reasons})
        else:
            new_id = len(kept)
            new_cluster_id[members] = new_id
            kept.append({**record, "cluster_id": new_id, **numbers})
    return new_cluster_id, kept, rejected, n_rejected_points


def normal_flatness(normals, tilt_deg=20.0):
    """
    The share (0 to 1) of the normals that are within `tilt_deg` degrees of
    the main normal of the group. The sign of a normal does not count
    (CloudCompare gives each normal an arbitrary sign).

    Why: the points of a damage site on a flat wall, floor or ceiling have
    nearly one normal direction. A false site along a join of two surfaces
    (wall and floor, wall and ceiling, a corner) holds two or three directions,
    so its share is low. Returns None when fewer than 3 normals are finite.
    """
    n = np.asarray(normals, dtype=np.float64)
    n = n[np.all(np.isfinite(n), axis=1)]
    if len(n) < 3:
        return None
    length = np.linalg.norm(n, axis=1)
    n = n[length > 1e-9] / length[length > 1e-9][:, None]
    if len(n) < 3:
        return None
    _w, vectors = np.linalg.eigh(n.T @ n)
    main = vectors[:, -1]
    angle = np.degrees(np.arccos(np.clip(np.abs(n @ main), 0.0, 1.0)))
    return float(np.mean(angle <= tilt_deg))


def site_shape_filter(xyz, normals, cluster_id, clusters, min_flatness, max_aspect,
                      tilt_deg=20.0):
    """
    Keeps the damage sites that have the shape of a damage on one surface.

    Two tests (a value of 0 switches a test off):
      min_flatness: the share of the points of the site whose normal is within
        `tilt_deg` degrees of the main normal of the site (see normal_flatness).
        A site that lies on a join of two surfaces has a low share.
      max_aspect: the long side divided by the short side of the site (both in
        the plane of the site). A strip along the foot of a wall is much longer
        than it is wide.

    Only the measured sites are tested (type "added", "removed" or "changed", or
    no type). Holes and unmatched sites are kept.
    normals: one row (nx, ny, nz) for each core point, or None (then the
    flatness test is not made).

    Returns (new_cluster_id, new_clusters, rejected, n_rejected_points) like
    site_size_filter. A kept record gets the keys "flatness" and "aspect" (a
    number, or None when it could not be found). A rejected record has the old
    id, "centroid", "point_count", the two numbers and "reasons".
    """
    new_cluster_id = np.full(len(cluster_id), -1, dtype=np.int64)
    kept, rejected = [], []
    n_rejected_points = 0
    for record in clusters:
        members = np.where(cluster_id == record["cluster_id"])[0]
        if record.get("type", "changed") not in ("added", "removed", "changed") \
                or not len(members):
            new_id = len(kept)
            new_cluster_id[members] = new_id
            kept.append({**record, "cluster_id": new_id})
            continue
        width, height, _t, _a, _p, _c, _b = measure_cluster(xyz[members])
        short, long_ = min(width, height), max(width, height)
        aspect = (long_ / short) if short > 1e-6 else float("inf")
        flatness = None
        if normals is not None and min_flatness > 0:
            flatness = normal_flatness(normals[members], tilt_deg)
        reasons = []
        if max_aspect > 0 and aspect > max_aspect:
            reasons.append(f"long side is {aspect:.1f} times the short side (limit {max_aspect:g})")
        if min_flatness > 0 and flatness is not None and flatness < min_flatness:
            reasons.append(f"normal share {flatness:.2f} < {min_flatness:.2f} "
                           f"(the site lies on more than one surface)")
        numbers = {"flatness": None if flatness is None else round(flatness, 3),
                   "aspect": round(aspect, 2) if np.isfinite(aspect) else None}
        if reasons:
            n_rejected_points += len(members)
            rejected.append({"cluster_id": record["cluster_id"],
                             "centroid": record["centroid"],
                             "point_count": record["point_count"], **numbers,
                             "reasons": reasons})
        else:
            new_id = len(kept)
            new_cluster_id[members] = new_id
            kept.append({**record, "cluster_id": new_id, **numbers})
    return new_cluster_id, kept, rejected, n_rejected_points


def unmatched_support_filter(xyz, labels, records, significant, valid, count2,
                             min_empty_share, min_changed_share, tree=None):
    """
    Keeps the "unmatched" sites that have a reason to be a real change.

    An unmatched site is a patch where the core points have no M3C2 distance but
    are not empty like a hole. Such a patch has two kinds of cause:
      - A real change: the surface moved by more than the M3C2 search depth, or
        it is not there in the second scan. Then the second scan has no points
        near the core points, or the valid core points around the patch show a
        significant change (the moved object has other faces).
      - A weak second scan on a surface that did not change (grazing angle, a
        join of surfaces): the second scan still has points, and the valid core
        points around the patch show no change.
    A site is kept when at least one of these is true:
      1. At least `min_empty_share` of its points have 0 points of the second
         scan in the M3C2 cylinder (`count2` is the count field; None = this test
         is not made).
      2. At least `min_changed_share` of the valid core points around it (within
         its radius + 5 cm, not its own points) have a significant change (Step A).
    Returns (new_labels, new_records, rejected). The labels and records are
    numbered again from 0 in the old order. A kept or rejected record gets
    "empty_share" and "changed_share" (the share, or None when it could not be
    found).
    """
    from scipy.spatial import cKDTree
    if tree is None:
        tree = cKDTree(xyz)
    new_labels = np.full(len(labels), -1, dtype=np.int64)
    kept, rejected = [], []
    for number, record in enumerate(records):
        members = np.where(labels == number)[0]
        empty_share = None
        if count2 is not None and len(members):
            empty_share = float(np.mean(np.asarray(count2)[members] == 0))
        radius = 0.5 * max(record.get("width", 0.0), record.get("height", 0.0)) + 0.05
        near = np.asarray(tree.query_ball_point(np.asarray(record["centroid"]), radius), dtype=np.int64)
        near = near[(labels[near] != number) & valid[near]]
        changed_share = float(np.mean(significant[near])) if len(near) >= 20 else None
        support = []
        if empty_share is not None and empty_share >= min_empty_share:
            support.append("empty")
        if changed_share is not None and changed_share >= min_changed_share:
            support.append("changed")
        numbers = {"empty_share": None if empty_share is None else round(empty_share, 3),
                   "changed_share": None if changed_share is None else round(changed_share, 3)}
        if support:
            new_labels[members] = len(kept)
            kept.append({**record, **numbers})
        else:
            rejected.append({"centroid": record["centroid"],
                             "point_count": record["point_count"], **numbers,
                             "reasons": ["the second scan has points here and the valid points "
                                         "around the site show no change"]})
    return new_labels, kept, rejected


# The values of 'auto' for the clustering. They come from the runs that were
# checked against the true damage (a core spacing of 3 cm: radius 8 cm, 4
# neighbours, smallest cluster 8) and are written as shares of the number of
# core points in the radius, so they follow the core spacing.
AUTO_EPS_FACTOR = 2.7          # radius = this x the median core spacing ...
AUTO_EPS_MIN, AUTO_EPS_MAX = 0.05, 0.08      # ... but between these two values (m)
AUTO_SAMPLES_SHARE = 0.29      # neighbours needed = this x the core points in the radius
AUTO_CLUSTER_SHARE = 0.57      # smallest cluster = this x the core points in the radius


def auto_cluster_settings(xyz, sample=3000, seed=0):
    """
    Finds the clustering values from the spacing of the core points.

    Returns a dict: spacing (median distance to the nearest other core point,
    m), eps (m), neighbours (the median number of core points in the radius,
    the point itself included), min_samples and min_cluster_size.
    Needs scipy.
    """
    from scipy.spatial import cKDTree
    xyz = np.asarray(xyz, dtype=np.float64)
    tree = cKDTree(xyz)
    rng = np.random.default_rng(seed)
    pick = rng.choice(len(xyz), min(len(xyz), sample), replace=False)
    distance, _ = tree.query(xyz[pick], k=2)
    spacing = float(np.median(distance[:, 1]))
    eps = float(np.clip(AUTO_EPS_FACTOR * spacing, AUTO_EPS_MIN, AUTO_EPS_MAX))
    counts = tree.query_ball_point(xyz[pick], eps, return_length=True)
    neighbours = float(np.median(counts))
    return {"spacing": spacing, "eps": eps, "neighbours": neighbours,
            "min_samples": max(4, int(round(AUTO_SAMPLES_SHARE * neighbours))),
            "min_cluster_size": max(4, int(round(AUTO_CLUSTER_SHARE * neighbours)))}


def _float_or_auto(text):
    """argparse type: a number, or the word 'auto'."""
    if str(text).strip().lower() == "auto":
        return "auto"
    try:
        return float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' is not a number or 'auto'")


def _int_or_auto(text):
    """argparse type: a whole number, or the word 'auto'."""
    if str(text).strip().lower() == "auto":
        return "auto"
    try:
        return int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' is not a whole number or 'auto'")


def read_confirmed_points(path):
    """The x, y, z of the points with cluster_id >= 0 in a Stage 6 output file."""
    vertex = PlyData.read(str(path))["vertex"]
    if "cluster_id" not in (vertex.data.dtype.names or ()):
        return np.zeros((0, 3))
    member = np.asarray(vertex["cluster_id"]) >= 0
    return np.column_stack([np.asarray(vertex[c], dtype=np.float64)[member]
                            for c in ("x", "y", "z")])


def second_run_points(args, other_file):
    """
    Runs this script on `other_file` (the second M3C2 result) with the same
    thresholds, uncertainty, background and clustering settings as this run, in a
    temporary folder, and returns the points of its damage sites (n x 3).
    Returns None and prints the reason when the run fails.
    """
    command = [sys.executable, str(Path(__file__).resolve()),
               "--input", str(other_file), "--keep-all", "--cluster-method", args.cluster_method,
               "--cluster-eps", str(args.cluster_eps),
               "--cluster-min-samples", str(args.cluster_min_samples),
               "--min-cluster-size", str(args.min_cluster_size)]
    if args.threshold is not None:
        command += ["--threshold", str(args.threshold)]
    if args.use_uncertainty:
        command.append("--use-uncertainty")
        if args.registration_error is not None:
            command += ["--registration-error", str(args.registration_error)]
            if args.registration_error_was is not None:
                command += ["--registration-error-was", str(args.registration_error_was)]
    if args.remove_background is not None:
        command += ["--remove-background", str(args.remove_background)]
    # The size limits (--min-site-*) are NOT passed on. At the other scale the same
    # damage can split into smaller parts. The second run must find all it can.
    with tempfile.TemporaryDirectory() as folder:
        out = Path(folder) / "second_run.ply"
        result = subprocess.run(command + ["--output", str(out)],
                                capture_output=True, text=True)
        if result.returncode != 0 or not out.exists():
            print("ERROR: the run on the second file (--persist-with) failed:")
            print((result.stdout + result.stderr).strip()[-1500:])
            return None
        return read_confirmed_points(out)


def robust_sigma(distances):
    """1.4826 x MAD about the median of the finite values, or None when there
    are fewer than 100 finite values. The same number as the 'robust_sigma' of
    Stage 5 (pipeline_core.robust_m3c2_spread)."""
    valid = distances[np.isfinite(distances)]
    if valid.size < 100:
        return None
    median = float(np.median(valid))
    return float(1.4826 * np.median(np.abs(valid - median)))


REGISTRATION_ERROR_MIN = 0.002      # m. The lowest value that 'auto' gives.


def rebase_registration_error(uncertainty, distances, requested, was):
    """Changes the registration part of the uncertainty field.

    CloudCompare writes uncertainty = 1.96 x (local roughness + registration
    error). With the registration error that Stage 5 used (`was`) and a new
    one (`new`), the new field is  uncertainty - 1.96 x (was - new). It never
    goes below 1.96 x new (the local roughness cannot be negative).

    requested: 'auto' (the robust spread of the distances, at least
    REGISTRATION_ERROR_MIN) or a number in metres. Returns (uncertainty,
    new). `new` is None, and the field is returned unchanged, when the change
    cannot be made (no `was`, no number, too few distances). The reason is
    printed."""
    if was is None:
        print("  NOTE: --registration-error needs --registration-error-was (the "
              "registration error of the Stage 5 run). The uncertainty field is not changed.")
        return uncertainty, None
    if str(requested).strip().lower() == "auto":
        sigma = robust_sigma(distances)
        if sigma is None:
            print("  NOTE: fewer than 100 valid distances, so the spread cannot be measured. "
                  "The uncertainty field is not changed.")
            return uncertainty, None
        new = max(sigma, REGISTRATION_ERROR_MIN)
        how = f"the robust spread of the distances ({sigma * 100:.2f} cm)"
    else:
        try:
            new = float(requested)
        except ValueError:
            print(f"  NOTE: --registration-error '{requested}' is not a number or 'auto'. "
                  f"The uncertainty field is not changed.")
            return uncertainty, None
        if new < 0:
            print("  NOTE: --registration-error must not be negative. The uncertainty field "
                  "is not changed.")
            return uncertainty, None
        how = "the value that was given"
    finite = np.isfinite(uncertainty)
    before = float(np.median(uncertainty[finite])) if finite.any() else float("nan")
    changed = np.where(finite, np.maximum(uncertainty - 1.96 * (was - new), 1.96 * new),
                       uncertainty)
    after = float(np.median(changed[finite])) if finite.any() else float("nan")
    print(f"  Registration error: {was * 100:.2f} cm (Stage 5) -> {new * 100:.2f} cm ({how}).")
    print(f"  Median uncertainty (LOD95): {before * 100:.2f} cm -> {after * 100:.2f} cm.")
    return changed, new


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


def summarize_clusters(flagged_points, flagged_distances, labels, cluster_type=None,
                       id_offset=0, flagged_change=None):
    """
    Step D: turns per-point cluster labels into one aggregate record per
    surviving cluster - centroid, point count, bounding extent (max-min
    per axis), mean and max M3C2 magnitude. This is what a report or
    operator-facing summary actually wants ("3 damage sites, sizes
    X/Y/Z"), not a raw list of thousands of flagged points.

    cluster_type, id_offset, flagged_change: used only with --find-holes.
    cluster_type ("added", "removed" or "changed") is written to each
    record as "type"; without it no "type" key is written, so the file
    keeps its original shape. id_offset is added to each cluster_id, so
    the added, removed and hole clusters of one run never share an id.
    flagged_change (change_inward of the same points) gives the record a
    signed "mean_change_inward".
    """
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
    new_data = np.empty(data.shape,
                        dtype=data.dtype.descr + [(name, dtype) for name, dtype, _v in columns])
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


def orient_by_probing(xyz, normals, voxel=0.05, reach=None, start=0.15,
                      sample_spacing=0.25, vote_radius=0.75, max_gap=0.6,
                      min_align=0.8, support_radius=0.15, support_points=8):
    """
    Finds, for each point, which side of its surface is the inside of the
    room - without an inside point.

    The scan sees a surface only from the room side. So from a point on a
    wall, a straight line into the room hits the opposite wall, and a line
    away from the room hits nothing. On a coarse set of sample points the
    function walks along the normal in both directions (from `start` to
    `reach` meters, in steps of `voxel`; reach is the diagonal of the cloud
    unless given) and counts the steps that are close to a point of the
    cloud. A step counts only when at least `support_points` points of the
    cloud are within `support_radius` of it (a surface has many points
    around a hit, a stray point has none), and only in runs of two or more.
    The side with clearly more hits is the inside. This works for an L-shaped room, where the centre of all points
    is not inside the room.

    Two things make the result robust:
      - A sample votes with the other samples on the same plane (within
        `vote_radius`). One bad line, for example next to a corner, is
        outvoted.
      - Each point of the cloud takes the side of the nearest decided
        sample with a similar normal (within `max_gap`).

    Returns (side, info). side is +1 where the normal points to the inside,
    -1 where it points away from the inside, 0 where the side could not be
    found (no decided sample is near, or the lines hit nothing on both
    sides).
    """
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
    v_nrm = normals[valid_idx] / np.linalg.norm(normals[valid_idx], axis=1, keepdims=True)

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
        reach = min(float(np.linalg.norm(v_xyz.max(axis=0) - v_xyz.min(axis=0))) + 0.5, 100.0)

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
                near = tree.query_ball_point(q[hit], r=support_radius, return_length=True,
                                             workers=-1)
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
    votes = d.astype(np.float64)               # a sample votes for itself
    if len(pairs):
        i, j = pairs[:, 0], pairs[:, 1]
        cos = np.einsum("ij,ij->i", s_nrm[i], s_nrm[j])
        off_ij = np.abs(np.einsum("ij,ij->i", s_xyz[j] - s_xyz[i], s_nrm[i]))
        off_ji = np.abs(np.einsum("ij,ij->i", s_xyz[i] - s_xyz[j], s_nrm[j]))
        same_plane = (np.abs(cos) > 0.9) & (off_ij < 0.05) & (off_ji < 0.05)
        i, j, cos = i[same_plane], j[same_plane], cos[same_plane]
        flip = np.where(cos >= 0, 1.0, -1.0)
        np.add.at(votes, i, d[j] * flip)       # j's side, in i's normal direction
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
        dist, nbr = ok_tree.query(v_xyz[a:b], k=k, distance_upper_bound=max_gap, workers=-1)
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
    """
    Turns the M3C2 distance so that its sign has the same meaning on every
    surface: + means the surface moved INTO the room (material added), -
    means it moved away from the room (material removed).

    CloudCompare turns all normals to one fixed direction (the M3C2 params
    file has NormalPreferedOri=0), so on the opposite wall the same real
    change has the opposite sign. Here each normal is turned to the inside
    first: change_inward = distance x sign(normal . (inside - point)).

    How the inside is found:
      - interior_point given: that point.
      - method "auto" (default): by probing, see orient_by_probing(). Points
        where this finds no side use the centre of all points.
      - method "centroid": the centre of all points for every point.

    Returns (change_inward, orient_info). change_inward has NaN where the
    distance is NaN. orient_info is a dict: method ("interior-point",
    "probe" or "centroid"), interior (the point used for the centre, or
    None), n_probe, n_fallback and disagree_with_centroid (share of points
    where probing and the centre of all points give different sides).
    Returns (None, None) when the file has no normals (nx, ny, nz) - the
    direction cannot be found then.
    """
    names = vertex.data.dtype.names or ()
    if not all(c in names for c in ("nx", "ny", "nz")):
        return None, None
    normals = np.column_stack([np.asarray(vertex[c], dtype=np.float64)
                               for c in ("nx", "ny", "nz")])
    if interior_point is not None:
        interior = np.asarray(interior_point, dtype=np.float64)
        method_used = "interior-point"
    else:
        interior = xyz.mean(axis=0)
        method_used = "centroid"
    facing = np.einsum("ij,ij->i", normals, interior - xyz)
    orient = np.where(facing >= 0.0, 1.0, -1.0)
    info = {"method": method_used, "interior": interior, "n_probe": 0,
            "n_fallback": 0, "disagree_with_centroid": 0.0}

    if interior_point is None and method == "auto":
        side, probe = orient_by_probing(xyz, normals)
        found = side != 0
        if found.any():
            info["method"] = "probe"
            info["n_probe"] = int(found.sum())
            info["n_fallback"] = int((~found).sum())
            info["disagree_with_centroid"] = float(
                (side[found] != np.where(orient[found] > 0, 1, -1)).mean())
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
    # A cluster with fewer than 3 points (a NaN group of 1 or 2 points) needs
    # full_matrices=True to give a 3 x 3 basis. With False the basis has fewer than
    # 3 rows and extent[2] raises IndexError (update 23). With 3 points or more,
    # full_matrices=False gives the same 3 x 3 basis. True would make an n x n
    # matrix: 12 GB for a cluster of 40 000 points (found 2026-10-17).
    _, _, vt = np.linalg.svd(points - center, full_matrices=len(points) < 3)
    local = (points - center) @ vt.T
    extent = local.max(axis=0) - local.min(axis=0)
    area = float(extent[0] * extent[1])
    perimeter = float(2.0 * (extent[0] + extent[1]))
    try:
        from scipy.spatial import ConvexHull
        hull = ConvexHull(local[:, :2])
        area = float(hull.volume)        # in 2D, "volume" is the area
        perimeter = float(hull.area)     # in 2D, "area" is the perimeter
    except Exception:
        pass
    return float(extent[0]), float(extent[1]), float(extent[2]), area, perimeter, center, vt


def ring_valid_fraction(cluster_points, tree, valid_mask, own_mask, ring):
    """Share of valid M3C2 values in a ring around the cluster.
    The ring holds all points within `ring` meters of the cluster,
    except the cluster itself."""
    from scipy.spatial import cKDTree
    radius = float(np.linalg.norm(cluster_points.max(axis=0) - cluster_points.min(axis=0))) + ring
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


def find_holes(xyz, valid, eps, min_samples, min_points, min_width, ring,
               min_ring_valid, min_fill, projection_radius=0.0,
               unmatched_min_fill=None):
    """
    Finds the holes: clusters of points that have no M3C2 distance (valid
    is False), kept only when they pass the four tests (see the module
    docstring). Returns (labels, holes, rejected, n_isolated):
      - labels: one int per point, -1 or the hole number (0 = the largest
        hole, sorted by area).
      - holes: one record per accepted hole, in the order of the labels.
      - rejected: records of the clusters that failed a test, each with
        "rejected_because".
      - n_isolated: missing points that are in no cluster at all.

    unmatched_min_fill: None (the default) = the old behavior, and the result
    is the 4-tuple above. A number (for example 0.05) = a cluster that fails
    ONLY the fill test, and has a fill share of at least this number, is kept
    as an "unmatched" site and not rejected. It is a patch with no partner
    in the other scan, but with valid points mixed in. A moved object looks
    like this: its faces have no partner, but its other faces and the points
    around it have a distance. The result is then the 6-tuple
    (labels, holes, rejected, n_isolated, unmatched_labels, unmatched):
    unmatched_labels is like labels (-1 or the site number, largest area
    first) and unmatched is one record for each site.
    """
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
            width, height, thickness, area, perimeter, center, basis = measure_cluster(pts)
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
                reasons.append(f"ring valid share {fraction:.2f} below {min_ring_valid}")
            if fill < min_fill:
                reasons.append(f"fill share {fill:.2f} below {min_fill}")
            only_fill = (len(reasons) == 1 and reasons[0].startswith("fill share"))
            if unmatched_min_fill is not None and only_fill and fill >= unmatched_min_fill:
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
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, help="M3C2 diff result .ply")
    parser.add_argument("--output", required=True, help="Filtered/classified .ply")
    parser.add_argument("--threshold", type=float, default=None,
                         help="Absolute distance (same units as the point cloud, "
                              "usually meters) above which a point counts as real "
                              "change. Points below this are dropped as noise. "
                              "Required unless --use-uncertainty is given - with "
                              "--use-uncertainty, this becomes optional and, if given, "
                              "acts as an extra floor on top of the per-point test "
                              "(a point must then clear both).")
    parser.add_argument("--use-uncertainty", action="store_true",
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
                              "M3C2 run.")
    parser.add_argument("--remove-background", type=float, default=None, metavar="R",
                         help="Subtract the smooth part of the M3C2 distances before the "
                              "tests. R is a radius in metres (for example 0.5). For every "
                              "core point the background is the median of the distances of "
                              "the core points within R on the same surface. A smooth "
                              "offset between two scans (SLAM drift) is as large as a small "
                              "damage, and it hides the damage or makes false sites. A "
                              "damage with sharp edges stays. A damage that covers more than "
                              "half of the circle of radius R is removed with the "
                              "background, so use an R that is larger than the largest "
                              "damage. Needs the normals (nx, ny, nz) in the input file. "
                              "The output file gets the field 'background' (metres). With "
                              "--registration-error auto, the spread is measured after the "
                              "background is removed. Default: off")
    parser.add_argument("--density-drop", type=float, default=None, metavar="R",
                         help="With --find-holes: also treat a core point as 'no data' when "
                              "the point count of the second scan in its projection cylinder "
                              "is less than R times the (median-scaled) count of the first "
                              "scan, for example 0.25. A hole in the second scan can still "
                              "give a distance when the cylinder reaches the points around "
                              "the hole. The drop in the count shows these holes. Needs the "
                              "two count fields that CloudCompare writes when the M3C2 "
                              "parameter ExportDensityAtProjScale is true (Stage 5 sets it "
                              "since update 23). Without the fields, this option does "
                              "nothing and prints a note. Default: off")
    parser.add_argument("--density-fields", nargs=2, metavar=("CLOUD1", "CLOUD2"), default=None,
                         help="The names of the two count fields (cloud 1, cloud 2) when "
                              "--density-drop cannot find them by itself.")
    parser.add_argument("--density-min-points", type=int, default=20, metavar="N",
                         help="--density-drop tests only core points with at least N points "
                              "of the first scan in the cylinder. Default: 20")
    parser.add_argument("--min-site-width", type=float, default=0.0, metavar="M",
                         help="Remove a measured damage site (added, removed or changed) "
                              "when its SHORT side, in the plane of the site, is smaller "
                              "than this (metres). The false sites of a SLAM map are often "
                              "thin lines along an edge or small patches. A limit in metres "
                              "does not depend on the point spacing. Holes and unmatched "
                              "sites are not tested. 0 = off (the old behaviour). "
                              "Default: 0")
    parser.add_argument("--min-site-area", type=float, default=0.0, metavar="M2",
                         help="Remove a measured damage site when the area of its outline "
                              "is smaller than this (square metres). 0 = off. Default: 0")
    parser.add_argument("--min-site-fill", type=float, default=0.0, metavar="S",
                         help="Remove a measured damage site when less than this share "
                              "(0 to 1) of the cells of a grid over the site hold a point. "
                              "The cell size is 2 times the median point spacing of the "
                              "site, so the number does not depend on the point spacing. "
                              "A thin or ring-like false site has a low share. 0 = off. "
                              "Default: 0")
    parser.add_argument("--min-site-flatness", type=float, default=0.0, metavar="S",
                         help="Remove a measured damage site when less than this share (0 to "
                              "1) of its points have a normal within 20 degrees of the main "
                              "normal of the site. A site on a join of two surfaces (wall and "
                              "floor, wall and ceiling, a corner) has several normal "
                              "directions and a low share. A damage on one flat surface has a "
                              "high share (a smooth dent or bulge gives about 0.65). Needs "
                              "the normals (nx, ny, nz). Holes and unmatched sites are not "
                              "tested. 0 = off. Default: 0")
    parser.add_argument("--max-site-aspect", type=float, default=0.0, metavar="R",
                         help="Remove a measured damage site when its long side is more than "
                              "R times its short side. A strip along the foot of a wall is "
                              "much longer than it is wide. 0 = off. Default: 0")
    parser.add_argument("--unmatched-support", action="store_true",
                         help="With --find-holes: keep an 'unmatched' site only when it has "
                              "a reason to be a real change: at least "
                              "--unmatched-empty-share of its points have no point of the "
                              "second scan in the M3C2 cylinder, or at least "
                              "--unmatched-changed-share of the valid core points around it "
                              "show a significant change. A site where the second scan has "
                              "points and the valid points around it show no change is a "
                              "weak second scan on a surface that did not change (grazing "
                              "angle, a join of surfaces). Off by default.")
    parser.add_argument("--unmatched-empty-share", type=float, default=0.25, metavar="S",
                         help="--unmatched-support: the smallest share of the points of the "
                              "site with 0 points of the second scan in the cylinder. "
                              "Default: 0.25")
    parser.add_argument("--unmatched-changed-share", type=float, default=0.5, metavar="S",
                         help="--unmatched-support: the smallest share of the valid core "
                              "points around the site with a significant change. "
                              "Default: 0.5")
    parser.add_argument("--persist-with", default=None, metavar="FILE",
                         help="A second M3C2 result of the SAME pair of scans, made with another "
                              "Normal scale (for example half of the first). Stage 6 runs on "
                              "this file too, with the same settings, and keeps a damage site "
                              "only if the second run also finds it. A site that comes from the "
                              "scale of one run (a smooth offset between the scans, noise on a "
                              "curved surface) changes with the scale, and real damage does "
                              "not. On the simulated SLAM maps this rejected about 40 %% of the "
                              "false sites and no damage. Holes and unmatched sites are not "
                              "tested. Needs scipy. The registration error of the second "
                              "file is taken as the same as the first (see "
                              "--registration-error-was). Default: off")
    parser.add_argument("--persist-distance", type=float, default=0.10, metavar="M",
                         help="--persist-with: how near (metres) a point of the second run must "
                              "be to a point of the site. Default: 0.10")
    parser.add_argument("--persist-share", type=float, default=0.5, metavar="S",
                         help="--persist-with: the share (0 to 1) of the points of a site that "
                              "need a point of the second run within --persist-distance. "
                              "Default: 0.5")
    parser.add_argument("--registration-error", default=None, metavar="M|auto",
                         help="[with --use-uncertainty] Replace the registration error that "
                              "Stage 5 put into the uncertainty field. A number is in metres. "
                              "'auto' is the robust spread (1.4826 x MAD about the median, "
                              "at least 0.2 cm) of the M3C2 distances of the input file. The "
                              "uncertainty field is 1.96 x (local roughness + registration "
                              "error), so only the registration part changes. This gives the "
                              "same result as a second Stage 5 run with the new registration "
                              "error, without CloudCompare. The ICP RMS of Stage 3 is often "
                              "much larger than the spread of the M3C2 distances, and a "
                              "registration error that is too large hides small damage. "
                              "Needs --registration-error-was. Default: off")
    parser.add_argument("--registration-error-was", type=float, default=None, metavar="M",
                         help="The registration error (metres) that the Stage 5 run used (the "
                              "RegistrationError line of its M3C2 params file). Needed by "
                              "--registration-error. Without it the uncertainty field is not "
                              "changed.")
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
    parser.add_argument("--cluster-eps", type=_float_or_auto, default=0.05,
                         help="[cluster, dbscan only] Max gap (meters) between flagged "
                              "points to still count as the same damage site. M3C2 core "
                              "point spacing is usually finer than a plane-segmentation "
                              "cloud's, so this defaults tighter than segment_planes.py's "
                              "--cluster-eps (0.15) - check core point spacing "
                              "(point_spacing.py) if sites are splitting or merging "
                              "unexpectedly. 'auto' = find it from the spacing of the core "
                              "points (see the note under --cluster-min-samples). "
                              "Default: 0.05")
    parser.add_argument("--cluster-min-samples", type=_int_or_auto, default=4,
                         help="[cluster] DBSCAN/HDBSCAN's density parameter: how many "
                              "flagged neighbors (within --cluster-eps, for dbscan) a "
                              "point needs to seed a cluster at all. 'auto' = find it from "
                              "the spacing of the core points: the radius is 2.7 times the "
                              "median core spacing (between 4 and 8 cm), the neighbours are "
                              "29 %% of the core points inside that radius (at least 4), and "
                              "the smallest cluster is 57 %% of them (at least 4). For a core "
                              "spacing of 3 cm this gives 8 cm, 4 and 8, the values that were "
                              "checked against true damage. A fixed value does not follow the "
                              "spacing: 2 cm and 50 neighbours find nothing in a cloud with a "
                              "5 mm spacing. 'auto' needs scipy. Default: 4")
    parser.add_argument("--min-cluster-size", type=_int_or_auto, default=4,
                         help="[cluster] A cluster's own tunable size floor, separate "
                              "from --cluster-min-samples - e.g. 'require at least 4 "
                              "flagged points total to count as a real site,' an "
                              "explicit, inspectable rejection criterion on top of "
                              "whatever --cluster-min-samples produced. Clusters "
                              "smaller than this are folded into noise (cluster_id=-1) "
                              "and dropped by Step C, same as any other noise point. "
                              "'auto' = see --cluster-min-samples. Default: 4")

    holes = parser.add_argument_group(
        "holes and direction of change",
        "Used only with --find-holes. See the module docstring.")
    holes.add_argument("--find-holes", action="store_true",
                       help="Also find holes (points with no M3C2 distance, where the new "
                            "scan has no points) and the direction of change (added or "
                            "removed). Adds the output fields change_inward and "
                            "change_type, and a 'type' for every cluster in the "
                            ".clusters.json file. Needs clustering (not --no-cluster) and "
                            "scipy. Default: off")
    holes.add_argument("--interior-point", type=float, nargs=3, metavar=("X", "Y", "Z"),
                       default=None,
                       help="A point inside the room, in the coordinates of the file. It "
                            "sets which side of a wall is the inside. Default: none - the "
                            "inside is found automatically (see --interior-method).")
    holes.add_argument("--interior-method", choices=["auto", "centroid"], default="auto",
                       help="How to find the inside of the room when --interior-point is "
                            "not given. auto: look along each normal - a line into the room "
                            "hits the opposite wall, a line away from the room hits nothing "
                            "(works for an L-shaped room); points with no clear answer use "
                            "the centre of all points. centroid: the centre of all points "
                            "for every point (correct only for a box-shaped room). "
                            "Default: auto")
    holes.add_argument("--projection-radius", type=float, default=0.0,
                       help="Radius (m) of the M3C2 projection cylinder: half of the "
                            "Search scale in the M3C2 params file. M3C2 finds valid values "
                            "up to this distance inside a hole, so the measured hole is "
                            "smaller than the real hole. With this value the hole records "
                            "also hold corrected sizes. Default: 0 (no correction)")
    holes.add_argument("--min-hole-points", type=int, default=100,
                       help="Smallest group of missing points that counts as a hole. "
                            "Default: 100")
    holes.add_argument("--min-hole-width", type=float, default=0.05,
                       help="Smallest short side (m) of a hole. This removes thin strips "
                            "along edges and corners. Default: 0.05")
    holes.add_argument("--hole-eps", type=float, default=0.05,
                       help="Largest gap (m) between missing points in one hole. "
                            "Default: 0.05")
    holes.add_argument("--hole-min-samples", type=int, default=4,
                       help="Density setting for the hole clustering. Default: 4")
    holes.add_argument("--ring", type=float, default=0.10,
                       help="Width (m) of the ring around a hole for the ring test. "
                            "Default: 0.10")
    holes.add_argument("--min-ring-valid", type=float, default=0.6,
                       help="Smallest share (0 to 1) of valid M3C2 values in the ring. A "
                            "low value means a shadow, not a hole. Default: 0.6")
    holes.add_argument("--min-fill", type=float, default=0.8,
                       help="Smallest share (0 to 1) of missing points inside the outline "
                            "of a hole. A low value means a patchy dropout area, not a "
                            "hole. Default: 0.8")
    holes.add_argument("--no-unmatched", dest="find_unmatched", action="store_false",
                       help="Turn off the 'unmatched' sites. By default, a cluster of points "
                            "without an M3C2 distance that fails ONLY the fill test (see "
                            "--min-fill) is kept as a site of the type 'unmatched' (a patch "
                            "with no partner in the other scan: a moved or absent object) "
                            "and not rejected. With this option, it is rejected as before.")
    holes.add_argument("--min-unmatched-fill", type=float, default=0.05,
                       help="Smallest fill share (0 to 1) of an 'unmatched' site. A cluster "
                            "below this share is rejected. Default: 0.05")
    holes.add_argument("--label-noise", action="store_true",
                       help="With --find-holes: give change_type 1 (added) or 2 (removed) "
                            "to EVERY flagged point, also the points that the clustering or "
                            "a site limit rejected (the behaviour before update 23). By "
                            "default only the points of a kept damage site have change_type "
                            "1 or 2, and all other points have 0. cluster_id is not "
                            "changed. Without clustering, all flagged points keep their "
                            "type.")
    holes.add_argument("--hole-value", type=float, default=0.10,
                       help="Value (m) that change_inward shows for the points of a hole. "
                            "It is for display only (colour scales). Default: 0.10")

    args = parser.parse_args()
    if args.threshold is None and not args.use_uncertainty:
        parser.error("--threshold is required unless --use-uncertainty is given.")
    if args.find_holes and not args.cluster:
        parser.error("--find-holes needs clustering. Do not use it with --no-cluster.")
    if args.min_site_width < 0 or args.min_site_area < 0:
        parser.error("--min-site-width and --min-site-area must not be negative.")
    if not 0.0 <= args.min_site_fill <= 1.0:
        parser.error("--min-site-fill must be between 0 and 1.")
    if not 0.0 <= args.min_site_flatness <= 1.0:
        parser.error("--min-site-flatness must be between 0 and 1.")
    if args.max_site_aspect < 0:
        parser.error("--max-site-aspect must not be negative.")
    if args.unmatched_support and not args.find_holes:
        parser.error("--unmatched-support needs --find-holes.")
    if not (0.0 <= args.unmatched_empty_share <= 1.0
            and 0.0 <= args.unmatched_changed_share <= 1.0):
        parser.error("--unmatched-empty-share and --unmatched-changed-share must be "
                     "between 0 and 1.")
    for _name, _value in (("--cluster-eps", args.cluster_eps),
                          ("--cluster-min-samples", args.cluster_min_samples),
                          ("--min-cluster-size", args.min_cluster_size)):
        if _value != "auto" and _value < 0:
            parser.error(f"{_name} must not be negative.")
    if args.persist_with:
        if not 0.0 <= args.persist_share <= 1.0:
            parser.error("--persist-share must be between 0 and 1.")
        if args.persist_distance <= 0:
            parser.error("--persist-distance must be larger than 0.")

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
    n_density_low = None
    if args.density_drop is not None:
        if not args.find_holes:
            print("  NOTE: --density-drop works only with --find-holes. The option is ignored.")
        elif not (0.0 < args.density_drop < 1.0):
            parser.error("--density-drop must be larger than 0 and smaller than 1.")
        else:
            names = vertex.data.dtype.names or ()
            pair = tuple(args.density_fields) if args.density_fields else \
                find_density_fields(names)
            if pair is None or pair[0] not in names or pair[1] not in names:
                print("  NOTE: --density-drop needs the two point-count fields of M3C2 "
                      "(ExportDensityAtProjScale=true), and the file has no such pair. "
                      f"Fields in the file: {list(names)}. Use --density-fields to name "
                      "them. The option is ignored.")
            else:
                low, median_ratio, n_tested = density_drop_mask(
                    vertex[pair[0]], vertex[pair[1]], args.density_drop, args.density_min_points)
                if median_ratio is None:
                    print(f"  NOTE: --density-drop: too few core points have {args.density_min_points} "
                          "or more points in the cylinder. The option is ignored.")
                else:
                    low &= finite_mask
                    n_density_low = int(low.sum())
                    print(f"  Density drop (fields '{pair[0]}' and '{pair[1]}'): the median "
                          f"ratio of the counts is {median_ratio:.2f}. {n_density_low} of "
                          f"{n_tested} tested core points have a count of cloud 2 below "
                          f"{args.density_drop:g} times the median ratio. They are treated as "
                          "points without a distance.")
                    distances = np.where(low, np.nan, distances)
                    finite_mask = np.isfinite(distances)
    n_invalid = int((~finite_mask).sum())
    if n_invalid:
        print(f"  Note: {n_invalid} points had no valid M3C2 distance"
              + (f" ({n_density_low} of them from the density drop; the others are outside "
                 f"max distance / insufficient neighbors)" if n_density_low else
                 " (outside max distance / insufficient neighbors)")
              + " and are treated as unchanged"
              + (" by Step A. --find-holes checks them below." if args.find_holes else "."))

    finite_distances = distances[finite_mask]
    if len(finite_distances) and (finite_distances >= 0).all():
        print("  WARNING: every value in the selected field is >= 0. A real M3C2 "
              "signed distance should straddle zero on unchanged surfaces - an "
              "all-positive field usually means this is actually an uncertainty "
              "or magnitude field, not the real distance field. Double check "
              "'Selected field' above against what you expect, and check the "
              "'Available fields' list for a better match if this looks wrong.")

    uncertainty_field_name = None
    uncertainty = None
    if args.use_uncertainty:
        uncertainty_field_name = find_uncertainty_field(vertex, field_name)
        if uncertainty_field_name is None:
            print(f"ERROR: --use-uncertainty given but no uncertainty field found. "
                  f"Available fields: {list(vertex.data.dtype.names or ())}")
            return 1
        print(f"  Uncertainty field: '{uncertainty_field_name}'")
        uncertainty = np.asarray(vertex[uncertainty_field_name], dtype=np.float64)
        finite_uncertainty = uncertainty[np.isfinite(uncertainty)]
        if len(finite_uncertainty) and (finite_uncertainty < 0).any():
            print("  WARNING: the selected uncertainty field has negative values - a "
                  "real uncertainty/magnitude field should never be negative. Double "
                  "check 'Uncertainty field' above against what you expect.")

    background = None
    if args.remove_background is not None:
        names = vertex.data.dtype.names or ()
        if not all(c in names for c in ("nx", "ny", "nz")):
            print("  NOTE: --remove-background needs the normals (nx, ny, nz) in the input "
                  "file, and the file has none. The distances are not changed.")
        elif args.remove_background <= 0:
            parser.error("--remove-background must be larger than 0.")
        else:
            xyz_bg = np.column_stack([np.asarray(vertex[c], dtype=np.float64)
                                      for c in ("x", "y", "z")])
            normals_bg = np.column_stack([np.asarray(vertex[c], dtype=np.float64)
                                          for c in ("nx", "ny", "nz")])
            try:
                background = estimate_background(xyz_bg, normals_bg, distances,
                                                 args.remove_background)
            except ImportError as e:
                print(f"ERROR: --remove-background needs scipy ({e}). Run: pip install scipy")
                return 1
            before = robust_sigma(distances)
            distances = distances - background
            after = robust_sigma(distances)
            moved = np.abs(background[finite_mask])
            print(f"  Background removed (radius {args.remove_background * 100:.0f} cm): "
                  f"median |background| {np.median(moved) * 100:.2f} cm, "
                  f"95 % below {np.percentile(moved, 95) * 100:.2f} cm.")
            if before is not None and after is not None:
                print(f"  Spread of the distances: {before * 100:.2f} cm -> {after * 100:.2f} cm.")
            del xyz_bg, normals_bg

    registration_error_used = None
    if args.use_uncertainty and args.registration_error is not None:
        uncertainty, registration_error_used = rebase_registration_error(
            uncertainty, distances, args.registration_error, args.registration_error_was)

    # --- Step A: magnitude threshold --------------------------------------
    if args.use_uncertainty:
        both_finite = finite_mask & np.isfinite(uncertainty)
        significant = both_finite & (np.abs(distances) > uncertainty)
        if args.threshold is not None:
            significant = significant & (np.abs(distances) >= args.threshold)
            mode_desc = (f"per-point uncertainty ('{uncertainty_field_name}') "
                         f"AND >= {args.threshold}")
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
        print(f"  Flagged distance range: {flagged_vals.min():.4f} to {flagged_vals.max():.4f}")
        print(f"  Flagged distance mean magnitude: {np.mean(np.abs(flagged_vals)):.4f}")
    else:
        if args.use_uncertainty:
            print("  WARNING: no points passed. Either nothing changed significantly, "
                  "or the combination of --use-uncertainty and --threshold is too "
                  "strict for this scan.")
        else:
            print("  WARNING: no points passed the threshold. Either nothing changed "
                  "significantly, or --threshold is set too high for this scan.")

    xyz = np.column_stack([np.asarray(vertex[c], dtype=np.float64) for c in ("x", "y", "z")])

    # --- 'auto' clustering values: from the spacing of the core points ------
    cluster_auto = None
    if "auto" in (args.cluster_eps, args.cluster_min_samples, args.min_cluster_size):
        if not args.cluster:
            args.cluster_eps = 0.05 if args.cluster_eps == "auto" else args.cluster_eps
            args.cluster_min_samples = 4 if args.cluster_min_samples == "auto" \
                else args.cluster_min_samples
            args.min_cluster_size = 4 if args.min_cluster_size == "auto" else args.min_cluster_size
        else:
            try:
                cluster_auto = auto_cluster_settings(xyz)
            except ImportError as e:
                print(f"ERROR: 'auto' needs scipy ({e}). Run: pip install scipy")
                return 1
            used = []
            if args.cluster_eps == "auto":
                args.cluster_eps = cluster_auto["eps"]
                used.append(f"radius {args.cluster_eps * 100:.1f} cm")
            if args.cluster_min_samples == "auto":
                args.cluster_min_samples = cluster_auto["min_samples"]
                used.append(f"neighbours {args.cluster_min_samples}")
            if args.min_cluster_size == "auto":
                args.min_cluster_size = cluster_auto["min_cluster_size"]
                used.append(f"smallest cluster {args.min_cluster_size}")
            print(f"\nClustering values 'auto': the median core spacing is "
                  f"{cluster_auto['spacing'] * 1000:.1f} mm, and a radius of "
                  f"{cluster_auto['eps'] * 100:.1f} cm holds {cluster_auto['neighbours']:.0f} core "
                  f"points. Used: {', '.join(used)}.")

    # --- Direction of change (--find-holes): added or removed ------------
    change_inward = None
    interior_used = None
    orient_info = None
    direction = np.zeros(n_total, dtype=np.int8)   # 0 none, 1 added, 2 removed
    if args.find_holes:
        change_inward, orient_info = orient_change_inward(
            vertex, xyz, distances, args.interior_point, args.interior_method)
        interior_used = orient_info["interior"] if orient_info else None
        if change_inward is None:
            print("\nNOTE: --find-holes: the file has no point normals (nx, ny, nz), so the "
                  "direction of change (added or removed) cannot be found. The damage sites "
                  "get the type 'changed', and the fields change_inward and change_type are "
                  "not written. Holes are still found.")
        else:
            direction[significant & (change_inward > 0)] = CHANGE_ADDED
            direction[significant & ~(change_inward > 0)] = CHANGE_REMOVED
            if orient_info["method"] == "probe":
                print(f"\nDirection of change: the inside of the room was found for "
                      f"{orient_info['n_probe']} points by looking along each normal "
                      f"(a line into the room hits the opposite wall).")
                if orient_info["n_fallback"]:
                    print(f"  {orient_info['n_fallback']} points have no clear answer. "
                          f"They use the centre of all points "
                          f"({', '.join(f'{v:.3f}' for v in interior_used)}).")
                share = orient_info["disagree_with_centroid"]
                if share > 0.02:
                    print(f"  The centre of all points would give the other side for "
                          f"{share * 100:.0f}% of the points. The room is probably not a "
                          f"simple box, and this is the reason for the automatic method.")
            else:
                where = ("--interior-point" if args.interior_point is not None
                         else "the centre of all points")
                print(f"\nDirection of change: the inside of the room is {where} "
                      f"({', '.join(f'{v:.3f}' for v in interior_used)}).")
            print(f"  Flagged as added: {int((direction == CHANGE_ADDED).sum())}, "
                  f"removed: {int((direction == CHANGE_REMOVED).sum())}")

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
            member = significant if kind is None else (significant & (direction == kind))
            flagged_indices = np.where(member)[0]
            if not len(flagged_indices):
                continue
            flagged_points = xyz[flagged_indices]
            flagged_distances = distances[flagged_indices]
            flagged_change = (change_inward[flagged_indices]
                              if (change_inward is not None and type_name) else None)

            labels = cluster_flagged_points(
                flagged_points, method_label, args.cluster_eps,
                args.cluster_min_samples, args.min_cluster_size)
            cluster_id[flagged_indices] = np.where(labels >= 0, labels + id_offset, -1)

            n_noise += int((labels < 0).sum())
            group_confirmed = int((labels >= 0).sum())
            n_confirmed += group_confirmed
            if type_name == "added":
                n_added_points = group_confirmed
            elif type_name == "removed":
                n_removed_points = group_confirmed
            part = summarize_clusters(flagged_points, flagged_distances, labels,
                                      cluster_type=type_name, id_offset=id_offset,
                                      flagged_change=flagged_change)
            clusters_summary += part
            id_offset += len(part)

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
            kind_text = f" [{c['type']}]" if "type" in c else ""
            print(f"    cluster {c['cluster_id']}{kind_text}: {c['point_count']} points, "
                  f"centroid=({centroid}), mean|d|={c['mean_magnitude']:.4f}, "
                  f"max|d|={c['max_magnitude']:.4f}")
    elif args.cluster and n_significant == 0:
        print("\nSkipping clustering: no points passed Step A.")

    # --- Size and fill limits (--min-site-width/-area/-fill) -----------------
    size_rejected = []
    n_size_rejected_points = 0
    size_filter_on = (args.min_site_width > 0 or args.min_site_area > 0
                      or args.min_site_fill > 0)
    if size_filter_on:
        if not args.cluster:
            print("\nNOTE: the size limits need clustering. The options are ignored.")
            size_filter_on = False
        elif clusters_summary:
            try:
                cluster_id, clusters_summary, size_rejected, n_size_rejected_points = \
                    site_size_filter(xyz, cluster_id, clusters_summary, args.min_site_width,
                                     args.min_site_area, args.min_site_fill)
            except ImportError as e:
                print(f"ERROR: the size limits need scipy ({e}). Run: pip install scipy")
                return 1
            n_confirmed -= n_size_rejected_points
            print(f"\nSize limits: short side >= {args.min_site_width * 100:g} cm, "
                  f"area >= {args.min_site_area * 1e4:g} cm2, fill >= {args.min_site_fill:g}. "
                  f"Sites kept: {len(clusters_summary)}, rejected: {len(size_rejected)} "
                  f"({n_size_rejected_points} points).")
            for record in size_rejected:
                c = ", ".join(f"{v:.2f}" for v in record["centroid"])
                print(f"    rejected: {record['point_count']} points at ({c}): "
                      f"{'; '.join(record['reasons'])}")
            if args.find_holes and change_inward is not None:
                n_added_points = int(sum(c["point_count"] for c in clusters_summary
                                         if c.get("type") == "added"))
                n_removed_points = int(sum(c["point_count"] for c in clusters_summary
                                           if c.get("type") == "removed"))

    # --- Shape limits (--min-site-flatness / --max-site-aspect) --------------
    shape_rejected = []
    n_shape_rejected_points = 0
    shape_filter_on = args.min_site_flatness > 0 or args.max_site_aspect > 0
    if shape_filter_on:
        if not args.cluster:
            print("\nNOTE: the shape limits need clustering. The options are ignored.")
            shape_filter_on = False
        elif clusters_summary:
            names_ = vertex.data.dtype.names or ()
            normals = None
            if all(c in names_ for c in ("nx", "ny", "nz")):
                normals = np.column_stack([np.asarray(vertex[c], dtype=np.float64)
                                           for c in ("nx", "ny", "nz")])
            elif args.min_site_flatness > 0:
                print("\nNOTE: --min-site-flatness needs the normals (nx, ny, nz). The file has "
                      "none. Only --max-site-aspect is used.")
            cluster_id, clusters_summary, shape_rejected, n_shape_rejected_points = \
                site_shape_filter(xyz, normals, cluster_id, clusters_summary,
                                  args.min_site_flatness, args.max_site_aspect)
            n_confirmed -= n_shape_rejected_points
            limits = []
            if args.min_site_flatness > 0 and normals is not None:
                limits.append(f"normal share >= {args.min_site_flatness:g}")
            if args.max_site_aspect > 0:
                limits.append(f"long side <= {args.max_site_aspect:g} x short side")
            print(f"\nShape limits: {', '.join(limits)}. Sites kept: {len(clusters_summary)}, "
                  f"rejected: {len(shape_rejected)} ({n_shape_rejected_points} points).")
            for record in shape_rejected:
                c = ", ".join(f"{v:.2f}" for v in record["centroid"])
                print(f"    rejected: {record['point_count']} points at ({c}): "
                      f"{'; '.join(record['reasons'])}")
            if args.find_holes and change_inward is not None:
                n_added_points = int(sum(c["point_count"] for c in clusters_summary
                                         if c.get("type") == "added"))
                n_removed_points = int(sum(c["point_count"] for c in clusters_summary
                                           if c.get("type") == "removed"))

    # --- Persistence (--persist-with): keep the sites that a second scale also finds
    persist_rejected = []
    n_persist_rejected_points = 0
    if args.persist_with:
        if not args.cluster:
            print("\nNOTE: --persist-with needs clustering. The option is ignored.")
        elif not clusters_summary:
            print("\nNOTE: --persist-with: there are no damage sites to test.")
        else:
            if not Path(args.persist_with).is_file():
                print(f"ERROR: --persist-with: no such file: {args.persist_with}")
                return 1
            print(f"\nPersistence: running on the second file {args.persist_with} ...")
            try:
                other_points = second_run_points(args, args.persist_with)
            except ImportError as e:
                print(f"ERROR: --persist-with needs scipy ({e}). Run: pip install scipy")
                return 1
            if other_points is None:
                return 1
            cluster_id, clusters_summary, persist_rejected, n_persist_rejected_points = \
                persistence_filter(xyz, cluster_id, clusters_summary, other_points,
                                   args.persist_distance, args.persist_share)
            n_confirmed -= n_persist_rejected_points
            print(f"  The second run has {len(other_points)} points in damage sites. "
                  f"Sites kept: {len(clusters_summary)}, rejected: {len(persist_rejected)} "
                  f"({n_persist_rejected_points} points).")
            for record in persist_rejected:
                c = ", ".join(f"{v:.2f}" for v in record["centroid"])
                print(f"    rejected: {record['point_count']} points at ({c}): the second run "
                      f"has points within {args.persist_distance:g} m for "
                      f"{record['persist_share'] * 100:.0f} % of them")
            if args.find_holes and change_inward is not None:
                n_added_points = int(sum(c["point_count"] for c in clusters_summary
                                         if c.get("type") == "added"))
                n_removed_points = int(sum(c["point_count"] for c in clusters_summary
                                           if c.get("type") == "removed"))

    # --- Holes (--find-holes): points without an M3C2 distance ------------
    hole_records = []
    hole_rejected = []
    unmatched_records = []
    unmatched_support_rejected = []
    n_hole_points = 0
    n_unmatched_points = 0
    hole_mask = np.zeros(n_total, dtype=bool)
    unmatched_mask = np.zeros(n_total, dtype=bool)
    if args.find_holes:
        print(f"\nLooking for holes: {n_invalid} points have no M3C2 distance...")
        try:
            found = find_holes(
                xyz, finite_mask, args.hole_eps, args.hole_min_samples,
                args.min_hole_points, args.min_hole_width, args.ring,
                args.min_ring_valid, args.min_fill, args.projection_radius,
                unmatched_min_fill=(args.min_unmatched_fill if args.find_unmatched else None))
            if len(found) == 4:          # --no-unmatched: the old result
                hole_labels, holes, hole_rejected, n_isolated = found
                unmatched_labels = np.full(n_total, -1, dtype=np.int64)
                unmatched = []
            else:
                (hole_labels, holes, hole_rejected, n_isolated,
                 unmatched_labels, unmatched) = found
        except ImportError as e:
            print(f"ERROR: --find-holes needs scipy and scikit-learn ({e}). "
                  f"Run: pip install scipy scikit-learn")
            return 1
        if args.find_unmatched and args.unmatched_support and unmatched:
            names_ = vertex.data.dtype.names or ()
            pair_ = tuple(args.density_fields) if args.density_fields else \
                find_density_fields(names_)
            count2 = None
            if pair_ is not None and pair_[1] in names_:
                count2 = np.asarray(vertex[pair_[1]], dtype=np.float64)
            else:
                print("  NOTE: --unmatched-support: the file has no point-count field of the "
                      "second scan. Only the test with the valid points around the site is used.")
            n_before = len(unmatched)
            unmatched_labels, unmatched, unmatched_support_rejected = unmatched_support_filter(
                xyz, unmatched_labels, unmatched, significant, finite_mask, count2,
                args.unmatched_empty_share, args.unmatched_changed_share)
            print(f"  Unmatched support: kept {len(unmatched)} of {n_before} unmatched sites.")
            for record in unmatched_support_rejected:
                c = ", ".join(f"{v:.2f}" for v in record["centroid"])
                print(f"    rejected: {record['point_count']} points at ({c}): "
                      f"{'; '.join(record['reasons'])}")
        first_hole_id = len(clusters_summary)
        for number, record in enumerate(holes):
            hole_records.append({"cluster_id": first_hole_id + number, "type": "hole", **record})
        hole_mask = hole_labels >= 0
        cluster_id[hole_mask] = hole_labels[hole_mask] + first_hole_id
        n_hole_points = int(hole_mask.sum())
        clusters_summary += hole_records

        # Patches with no partner that fail only the fill test: moved or absent objects.
        first_unmatched_id = len(clusters_summary)
        for number, record in enumerate(unmatched):
            unmatched_records.append(
                {"cluster_id": first_unmatched_id + number, "type": "unmatched", **record})
        unmatched_mask = unmatched_labels >= 0
        cluster_id[unmatched_mask] = unmatched_labels[unmatched_mask] + first_unmatched_id
        n_unmatched_points = int(unmatched_mask.sum())
        clusters_summary += unmatched_records

        print(f"  Holes accepted: {len(hole_records)} ({n_hole_points} points)")
        for record in hole_records:
            c = ", ".join(f"{v:.3f}" for v in record["centroid"])
            print(f"    cluster {record['cluster_id']} [hole]: {record['point_count']} points, "
                  f"centroid=({c}), size {record['width']:.2f} x {record['height']:.2f} m, "
                  f"area {record['area']:.3f} m2, ring valid "
                  f"{record['ring_valid_fraction']:.2f}, fill {record['fill_fraction']:.2f}")
            if "corrected_area" in record:
                print(f"        corrected for the projection radius: "
                      f"{record['corrected_width']:.2f} x {record['corrected_height']:.2f} m, "
                      f"area {record['corrected_area']:.3f} m2")
        if args.find_unmatched:
            print(f"  Unmatched sites (no partner in the other scan, but not empty "
                  f"like a hole): {len(unmatched_records)} ({n_unmatched_points} points)")
            for record in unmatched_records:
                c = ", ".join(f"{v:.3f}" for v in record["centroid"])
                print(f"    cluster {record['cluster_id']} [unmatched]: "
                      f"{record['point_count']} points, centroid=({c}), size "
                      f"{record['width']:.2f} x {record['height']:.2f} m, fill "
                      f"{record['fill_fraction']:.2f}")
        print(f"  Clusters rejected as not a hole: {len(hole_rejected)}")
        for record in hole_rejected:
            c = ", ".join(f"{v:.2f}" for v in record["centroid"])
            print(f"    rejected: {record['point_count']} points at ({c}): "
                  f"{'; '.join(record['rejected_because'])}")
        print(f"  Missing points in no cluster at all: {n_isolated}")

    # Fields that --find-holes adds to the output cloud.
    n_unlabelled = 0
    change_columns = []
    if args.find_holes and change_inward is not None:
        shown = change_inward.astype(np.float32)
        shown[hole_mask] = -abs(args.hole_value)
        kind_field = direction.astype(np.int32)
        n_unlabelled = 0
        if args.cluster and not args.label_noise:
            # Only the points of a kept site have the type added or removed. The
            # flagged points that the clustering or a site limit rejected get 0.
            rejected_flag = (kind_field != CHANGE_NONE) & (cluster_id < 0)
            n_unlabelled = int(rejected_flag.sum())
            kind_field[rejected_flag] = CHANGE_NONE
        kind_field[hole_mask] = CHANGE_HOLE
        kind_field[unmatched_mask] = CHANGE_UNMATCHED
        change_columns = [("change_inward", "f4", shown), ("change_type", "i4", kind_field)]
        if n_unlabelled:
            print(f"\nchange_type: {n_unlabelled} flagged points are in no kept damage site. "
                  f"They get change_type 0 (use --label-noise for the old labels).")

    # --- Write output cloud ------------------------------------------------
    if args.keep_all:
        columns = [("classified", "i4", np.asarray(significant, dtype=np.int32))]
        if background is not None:
            columns.append(("background", "f4", background.astype(np.float32)))
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
            note = ("flagged AND spatially confirmed (Step A + Step B/C)"
                    + (", plus the points of holes" if args.find_holes else ""))
        else:
            keep_mask = significant
            note = "flagged (Step A only)"
        filtered_data = vertex.data[keep_mask]
        if args.cluster:
            # Same "i4" not "i8" fix as the keep_all branch above.
            columns = [("cluster_id", "i4", cluster_id[keep_mask].astype(np.int32))]
            columns += [(name, t, values[keep_mask]) for name, t, values in change_columns]
            if background is not None:
                columns.append(("background", "f4", background[keep_mask].astype(np.float32)))
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
            "cluster_eps": args.cluster_eps if args.cluster_method == "dbscan" else None,
            "cluster_min_samples": args.cluster_min_samples,
            "min_cluster_size": args.min_cluster_size,
            "n_total": n_total,
            "n_flagged": n_significant,
            "n_noise": n_noise,
            "n_confirmed": n_confirmed,
            "clusters": clusters_summary,
        }
        if size_filter_on:
            summary["min_site_width"] = args.min_site_width
            summary["min_site_area"] = args.min_site_area
            summary["min_site_fill"] = args.min_site_fill
            summary["n_size_rejected"] = len(size_rejected)
            summary["n_size_rejected_points"] = n_size_rejected_points
            summary["size_rejected"] = size_rejected
        if cluster_auto is not None:
            summary["cluster_auto"] = {k: (round(v, 5) if isinstance(v, float) else v)
                                       for k, v in cluster_auto.items()}
        if shape_filter_on:
            summary["min_site_flatness"] = args.min_site_flatness
            summary["max_site_aspect"] = args.max_site_aspect
            summary["n_shape_rejected"] = len(shape_rejected)
            summary["n_shape_rejected_points"] = n_shape_rejected_points
            summary["shape_rejected"] = shape_rejected
        if args.persist_with:
            summary["persist_with"] = str(args.persist_with)
            summary["persist_distance"] = args.persist_distance
            summary["persist_share"] = args.persist_share
            summary["n_persist_rejected"] = len(persist_rejected)
            summary["n_persist_rejected_points"] = n_persist_rejected_points
            summary["persist_rejected"] = persist_rejected
        if n_density_low is not None:
            summary["density_drop"] = args.density_drop
            summary["n_density_low"] = n_density_low
        if args.remove_background is not None:
            summary["remove_background"] = (args.remove_background
                                            if background is not None else None)
        if registration_error_used is not None:
            summary["registration_error_was"] = args.registration_error_was
            summary["registration_error_used"] = registration_error_used
        if args.find_holes:
            # n_confirmed, n_noise and n_flagged keep their meaning: measured
            # change only. The points of holes are counted on their own.
            summary.update({
                "find_holes": True,
                "direction_found": change_inward is not None,
                "interior_point": interior_used.tolist() if interior_used is not None else None,
                "interior_method": orient_info["method"] if change_inward is not None else None,
                "n_orient_fallback": orient_info["n_fallback"] if change_inward is not None else None,
                "orient_disagree_with_centroid": (
                    round(orient_info["disagree_with_centroid"], 4)
                    if change_inward is not None else None),
                "n_added_points": n_added_points,
                "n_removed_points": n_removed_points,
                "n_holes": len(hole_records),
                "n_hole_points": n_hole_points,
                "label_noise": bool(args.label_noise),
                "n_unlabelled_flagged": n_unlabelled,
                "unmatched_support": bool(args.unmatched_support),
                "n_unmatched_support_rejected": len(unmatched_support_rejected),
                "unmatched_support_rejected": unmatched_support_rejected,
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
            })
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"Cluster summary saved to: {summary_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
