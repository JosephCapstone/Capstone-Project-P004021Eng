#!/usr/bin/env python3
"""
usd_export.py
==============
Converts a cleaned baseline point cloud and an M3C2 change-highlight point
cloud into a USD scene for use in NVIDIA Omniverse (or any USD viewer).

Scene layout:
    /World
      /Compartment
        /Baseline         - muted grey, full environment context, small points
                             (or a mesh, if given one - e.g. from running
                             surface_reconstruction.py manually)
        /ChangeHighlight   - diverging blue-white-red colormap driven by the
                             M3C2 distance scalar field, larger points
                             (or a mesh with the field carried through)
        /DamageDetail      - optional. Real comparison-cloud geometry near
                             flagged locations (from extract_damage_detail.py),
                             colored the same way. Unlike ChangeHighlight
                             (a magnitude value plotted at baseline positions,
                             since M3C2's core points are baseline-sourced),
                             this shows what damage/debris actually looks
                             like right now.
        /FlaggedPoints     - optional (--flagged). Only the flagged points of
                             a Stage 6 (Classify) output, coloured like
                             ChangeHighlight. Use it when Classify ran with
                             'Keep all points': Stage 7 then meshes the whole
                             surface for ChangeHighlight, and this layer
                             shows only the damage (see select_flagged()).
        /DamageSites       - optional (--clusters). One marker per damage
                             site from m3c2_classify.py's <name>.clusters.json:
                             a see-through box at the site's centroid, sized
                             to its extent, red by its max magnitude. Each
                             site stores its numbers as 'delta:' attributes
                             (see add_damage_sites()). With --surfaces, each
                             site also gets the surface it sits on (wall_2,
                             floor, ...) from Stage 4 (Segment).

Units and axes: the stage is written in metres (metersPerUnit = 1) with
Z up, and /World is the default prim. Without metersPerUnit, a USD reader
uses its default of 0.01 (centimetres), so Omniverse/Isaac Sim can show
the compartment 100 times too small (update 6 fix).

Accepts either a plain point cloud or a mesh (auto-detected via presence
of a 'face' element in the PLY) for --baseline and --change independently
- so you can mix and match, e.g. a meshed baseline with a still-points
change-highlight, or both as meshes.

Requires:
    pip install usd-core plyfile numpy scipy   (scipy only for --surfaces)

Usage (matches what the pipeline applet's Stage 6 calls):
    python usd_export.py --baseline baseline.ply --change change.ply --output scene.usd
    python usd_export.py --baseline baseline.ply --change change.ply --output scene.usd --usdz
    python usd_export.py --baseline baseline.ply --change change.ply --output scene.usd \
        --clusters classified.clusters.json
    python usd_export.py --baseline baseline.ply --change change.ply --output scene.usd \
        --clusters classified.clusters.json --surfaces segment_classified.ply
    python usd_export.py --baseline baseline.ply --change surface_mesh.ply --output scene.usd \
        --flagged classified_keep_all.ply

Confirmed working for point clouds: producing a valid, readable .usd
(checked via Usd.Stage.Open + ExportToString). Mesh support (UsdGeom.Mesh
instead of UsdGeom.Points) is new and hasn't been run against real data
yet. --usdz packaging is also unconfirmed - check the console output on
first use of either.
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from plyfile import PlyData
from pxr import Gf, Sdf, Usd, UsdGeom, UsdUtils, Vt

BASELINE_DISPLAY_COLOR = (0.5, 0.5, 0.5)     # muted grey
BASELINE_POINT_WIDTH = 0.01                   # meters
CHANGE_POINT_WIDTH = 0.03                     # meters, larger so it stands out
FALLBACK_HIGHLIGHT_COLOR = (1.0, 0.6, 0.0)    # used only if no scalar field is found
SITE_MIN_SIZE = 0.02                          # meters - a flat site still gets a visible box
SITE_OPACITY = 0.35                           # see-through, so the geometry inside shows
SURFACE_MATCH_DISTANCE = 0.05                 # meters - a site point farther than this from
                                              # every Segment point gets no surface label


def find_distance_field(vertex):
    """
    Finds CloudCompare's M3C2 signed-distance field, not its uncertainty
    field - both contain 'distance' in the name (e.g. 'M3C2 distance' vs
    'distance_uncertainty'), and a naive substring search on 'distance'
    matches the wrong one, silently. Uncertainty values are always
    positive with no meaningful zero-crossing, which caused every point
    to look "significant" downstream on a real run - same bug, same fix,
    as m3c2_classify.py.

    Priority order:
    1. Exact match for CloudCompare's standard name 'M3C2 distance'
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


def read_ply_data(path, warn_if_missing=False):
    """
    Returns (positions Nx3 float32, scalar_field N float32 or None,
    faces Nx3 int32 or None).

    faces is None for a plain point cloud PLY, or an Nx3 array of vertex
    indices if the PLY has a 'face' element - i.e. it's a mesh, such as
    output from surface_reconstruction.py. The caller uses this to decide
    between building a UsdGeom.Points or UsdGeom.Mesh prim.

    Looks for CloudCompare's M3C2 distance field via find_distance_field()
    above. Returns None for the scalar field if nothing matches - the
    caller falls back to a flat highlight color in that case.

    warn_if_missing: only print the "no field found" note when a distance
    field was actually expected (the change-highlight file) - the baseline
    file never has one and isn't supposed to, so staying quiet there avoids
    a misleading warning on a perfectly normal read.
    """
    ply = PlyData.read(str(path))
    if "vertex" not in ply:
        raise ValueError(f"'{path}' has no vertex data - is this really a point cloud PLY?")

    vertex = ply["vertex"]
    positions = np.stack(
        [vertex["x"], vertex["y"], vertex["z"]], axis=-1
    ).astype(np.float32)

    scalar_field = None
    field_name = find_distance_field(vertex)
    if field_name:
        scalar_field = np.asarray(vertex[field_name], dtype=np.float32)
        print(f"  Using scalar field '{field_name}' for coloring.")
        finite = scalar_field[np.isfinite(scalar_field)]
        if len(finite) and (finite >= 0).all():
            print(f"  WARNING: every value in '{field_name}' is >= 0 - a real M3C2 "
                  f"signed distance should straddle zero. This may be an uncertainty "
                  f"or magnitude field rather than the real distance field.")
    elif warn_if_missing:
        print(f"  No M3C2/distance field found. Available fields: {list(vertex.data.dtype.names or ())}")

    faces = None
    if "face" in ply:
        raw_faces = ply["face"]["vertex_indices"]
        # plyfile represents a variable-length list property as an object
        # array of per-face index arrays; triangle meshes from
        # surface_reconstruction.py are always exactly 3 per face, so this
        # stacks cleanly into a fixed Nx3 array.
        faces = np.stack([np.asarray(f, dtype=np.int32) for f in raw_faces])
        print(f"  Found {len(faces)} mesh faces - will build a Mesh prim, not Points.")

    return positions, scalar_field, faces


def diverging_colormap(values, clip_percentile=98):
    """
    Maps signed distance values to a blue (negative) - white (zero) - red
    (positive) diverging colormap. The color range is scaled from the
    data's own magnitude (robust to outliers via a percentile clip) rather
    than a hardcoded distance unit, since compartment scale/units may vary.
    """
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return None

    limit = max(float(np.percentile(np.abs(finite), clip_percentile)), 1e-6)
    normalized = np.clip(np.nan_to_num(values) / limit, -1.0, 1.0)

    colors = np.ones((len(values), 3), dtype=np.float32)  # start white
    neg = normalized < 0
    pos = normalized >= 0

    t_neg = -normalized[neg]
    colors[neg, 0] = 1.0 - t_neg
    colors[neg, 1] = 1.0 - t_neg
    colors[neg, 2] = 1.0

    t_pos = normalized[pos]
    colors[pos, 0] = 1.0
    colors[pos, 1] = 1.0 - t_pos
    colors[pos, 2] = 1.0 - t_pos

    return colors


def to_vec3f_array(np_array):
    """Vt.Vec3fArray.FromNumpy exists on recent USD builds; fall back to a
    manual conversion for older ones so this doesn't hard-fail on an
    unexpected usd-core version."""
    try:
        return Vt.Vec3fArray.FromNumpy(np_array.astype(np.float32))
    except AttributeError:
        return Vt.Vec3fArray([tuple(p) for p in np_array])


def add_point_cloud(stage, prim_path, positions, colors, point_width):
    points_prim = UsdGeom.Points.Define(stage, prim_path)
    points_prim.CreatePointsAttr(to_vec3f_array(positions))
    points_prim.CreateWidthsAttr(Vt.FloatArray([point_width] * len(positions)))

    color_primvar = points_prim.CreateDisplayColorPrimvar(UsdGeom.Tokens.vertex)
    color_primvar.Set(to_vec3f_array(colors))

    return points_prim


def add_mesh(stage, prim_path, positions, colors, faces):
    """Builds a UsdGeom.Mesh prim from a triangle mesh (positions + Nx3
    face indices, e.g. from surface_reconstruction.py). Vertex color uses
    the same 'vertex' interpolation as add_point_cloud, so per-vertex
    carried scalar fields (like M3C2 distance) still work the same way."""
    mesh_prim = UsdGeom.Mesh.Define(stage, prim_path)
    mesh_prim.CreatePointsAttr(to_vec3f_array(positions))
    mesh_prim.CreateFaceVertexCountsAttr(Vt.IntArray([3] * len(faces)))
    mesh_prim.CreateFaceVertexIndicesAttr(Vt.IntArray([int(i) for i in faces.flatten()]))

    color_primvar = mesh_prim.CreateDisplayColorPrimvar(UsdGeom.Tokens.vertex)
    color_primvar.Set(to_vec3f_array(colors))

    return mesh_prim


def package_as_usdz(usd_path, usdz_path):
    """
    Wraps a saved .usd/.usda file into a single .usdz package - the format
    most web/AR/mobile viewers expect (many reject a raw .usd outright even
    when it's valid, since they're built around the zipped-package
    convention). Uses UsdUtils directly rather than shelling out to the
    separate usdzip tool, since that's one more executable that could have
    its own PATH problems - this only needs the pxr module already in use.
    Returns True on success.
    """
    success = UsdUtils.CreateNewUsdzPackage(Sdf.AssetPath(str(usd_path)), str(usdz_path))
    if success:
        print(f"Packaged as .usdz: {usdz_path}")
    else:
        print(f"  WARNING: .usdz packaging failed for an unknown reason. "
              f"The .usd file itself ({usd_path}) is still valid and usable.")
    return success


def voxel_downsample(positions, colors, voxel_size):
    """
    Reduces point count via voxel-grid binning - keeps one representative
    point (and its color) per occupied voxel cell, dropping the rest.

    This isn't the same as losing genuine detail: dense LiDAR captures
    typically have a lot of near-duplicate points from overlapping scan
    passes, closer together than any real feature size. As long as
    voxel_size stays smaller than the smallest feature you actually care
    about seeing, this removes that redundancy without visibly changing
    what the scene shows - it's export-time only, applied after every
    upstream stage (M3C2, classification, etc.) has already run at full
    density, so detection accuracy is unaffected.

    Vectorized (no per-point Python loop) - picks whichever point sorts
    first within each occupied cell, which is a negligible difference
    from picking the true cell centroid as long as voxel_size is
    reasonable, since every point in a cell is within voxel_size of every
    other point in that same cell by construction.
    """
    if not voxel_size or voxel_size <= 0 or len(positions) == 0:
        return positions, colors

    voxel_indices = np.floor(positions / voxel_size).astype(np.int64)
    _, keep_idx = np.unique(voxel_indices, axis=0, return_index=True)
    return positions[keep_idx], colors[keep_idx]


def add_layer(stage, prim_path, ply_path, point_width, warn_if_missing=False,
              fallback_color=None, uniform_color=None, voxel_size=None):
    """
    Reads a PLY (point cloud or mesh, auto-detected via read_ply_data) and
    adds it to the stage as either a UsdGeom.Mesh or UsdGeom.Points prim.

    uniform_color: fixed color regardless of any scalar field (baseline).
    fallback_color: used only if uniform_color is None AND no scalar field
    was found (change-highlight / damage-detail falling back when their
    expected M3C2 field is missing).
    voxel_size: optional - downsamples point clouds before writing (see
    voxel_downsample() above). Only applies to point clouds; meshes are
    left untouched, since removing vertices arbitrarily would break face
    topology (holes, degenerate triangles).
    """
    pos, scalar_field, faces = read_ply_data(ply_path, warn_if_missing=warn_if_missing)

    if uniform_color is not None:
        colors = np.tile(uniform_color, (len(pos), 1)).astype(np.float32)
    elif scalar_field is not None:
        colors = diverging_colormap(scalar_field)
    else:
        print("  WARNING: falling back to a flat highlight color. If you expected "
              "a magnitude gradient: for a point cloud, double check this file is "
              "the actual M3C2 result (or extract_damage_detail.py output), not "
              "one of the duplicate input-cloud copies CloudCompare also saves "
              "during Stage 4. For a mesh, make sure surface_reconstruction.py was "
              "run with --carry-field set.")
        colors = np.tile(fallback_color, (len(pos), 1)).astype(np.float32)

    if faces is not None:
        add_mesh(stage, prim_path, pos, colors, faces)
        print(f"  {len(pos)} vertices / {len(faces)} faces added (mesh).")
    else:
        if voxel_size:
            n_before = len(pos)
            pos, colors = voxel_downsample(pos, colors, voxel_size)
            print(f"  Downsampled {n_before} -> {len(pos)} points "
                  f"(voxel size {voxel_size}).")
        add_point_cloud(stage, prim_path, pos, colors, point_width)
        print(f"  {len(pos)} points added.")


def select_flagged(path):
    """Reads a Stage 6 (Classify) output and returns (positions Nx3
    float32, scalar_field N float32 or None, rule) for its flagged points
    only. rule is a short text that tells which field was used:

      - 'cluster_id' field: points with cluster_id >= 0 (flagged AND in a
        damage site - the same points a Classify run without 'Keep all
        points' writes).
      - else 'classified' field: points with classified == 1 (passed the
        distance threshold; Classify ran with clustering off).
      - else: all points (the file has only flagged points already).
    """
    vertex = PlyData.read(str(path))["vertex"]
    names = vertex.data.dtype.names or ()
    positions = np.stack([vertex["x"], vertex["y"], vertex["z"]], axis=-1).astype(np.float32)
    if "cluster_id" in names:
        keep = np.asarray(vertex["cluster_id"]) >= 0
        rule = "cluster_id >= 0"
    elif "classified" in names:
        keep = np.asarray(vertex["classified"]) == 1
        rule = "classified = 1"
    else:
        keep = np.ones(len(positions), dtype=bool)
        rule = "all points (no flag field)"
    field_name = find_distance_field(vertex)
    scalar_field = (np.asarray(vertex[field_name], dtype=np.float32)[keep]
                    if field_name else None)
    return positions[keep], scalar_field, rule


def add_flagged_layer(stage, prim_path, path, voxel_size=None):
    """Adds the flagged points of a Classify output as a Points prim,
    coloured by the M3C2 field (or FALLBACK_HIGHLIGHT_COLOR without one).
    Returns the number of points added."""
    positions, scalar_field, rule = select_flagged(path)
    print(f"  {len(positions)} flagged point(s) ({rule}).")
    if not len(positions):
        print("  NOTE: no flagged points - the FlaggedPoints layer is empty.")
    if scalar_field is not None and len(positions):
        colors = diverging_colormap(scalar_field)
    else:
        colors = np.tile(FALLBACK_HIGHLIGHT_COLOR, (len(positions), 1)).astype(np.float32)
    if voxel_size and len(positions):
        n_before = len(positions)
        positions, colors = voxel_downsample(positions, colors, voxel_size)
        print(f"  Downsampled {n_before} -> {len(positions)} points (voxel size {voxel_size}).")
    add_point_cloud(stage, prim_path, positions, colors, CHANGE_POINT_WIDTH)
    return len(positions)


def load_clusters(path):
    """Reads m3c2_classify.py's <name>.clusters.json. Returns the whole
    summary dict. Raises ValueError with a clear message if the file is
    not a cluster summary."""
    try:
        summary = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"Could not read the cluster summary '{path}': {e}")
    if not isinstance(summary, dict) or not isinstance(summary.get("clusters"), list):
        raise ValueError(f"'{path}' is not a cluster summary from m3c2_classify.py "
                         f"(no 'clusters' list).")
    return summary


def load_surface_cloud(path):
    """Reads a Stage 4 (Segment) cloud - <name>_classified.ply or
    <name>_envelope_filtered.ply - and the manifest.json beside it.

    Returns (positions Nx3 float64, classification N int32, names) where
    names maps each classification number to its surface name (0 =
    'unclassified', 1 = 'floor', ...). Without a manifest.json, a number
    other than 0 is named 'surface_<n>'.

    Raises ValueError if the file has no 'classification' field."""
    ply = PlyData.read(str(path))
    if "vertex" not in ply:
        raise ValueError(f"'{path}' has no vertex data.")
    vertex = ply["vertex"]
    field_names = vertex.data.dtype.names or ()
    field = next((n for n in field_names
                  if n.lower() in ("classification", "scalar_classification")), None)
    if field is None:
        raise ValueError(
            f"'{path}' has no 'classification' field. Use a Stage 4 (Segment) output "
            f"(<name>_classified.ply or <name>_envelope_filtered.ply).")
    positions = np.stack([vertex["x"], vertex["y"], vertex["z"]], axis=-1).astype(np.float64)
    classification = np.rint(np.asarray(vertex[field], dtype=np.float64)).astype(np.int32)

    names = {0: "unclassified"}
    manifest_path = Path(path).parent / "manifest.json"
    if manifest_path.is_file():
        try:
            ids = json.loads(manifest_path.read_text(encoding="utf-8")).get(
                "classification_ids") or {}
            names.update({int(k): str(v) for k, v in ids.items()})
        except (OSError, ValueError, AttributeError):
            print(f"  WARNING: could not read surface names from {manifest_path}.")
    else:
        print(f"  NOTE: no manifest.json beside {path} - surfaces are named by number.")
    return positions, classification, names


def find_site_points(clusters_path, summary):
    """Finds the Classify output that the cluster summary belongs to (the
    .ply beside it with the same name: X.clusters.json -> X.ply, or the
    summary's own 'output' entry) and returns (positions Nx3 float64,
    cluster_id N int32) for its points. Returns None if that file is not
    found or has no cluster_id field."""
    candidates = [Path(clusters_path).with_suffix("").with_suffix(".ply")]
    if summary.get("output"):
        candidates.append(Path(summary["output"]))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        vertex = PlyData.read(str(candidate))["vertex"]
        if "cluster_id" not in (vertex.data.dtype.names or ()):
            continue
        positions = np.stack([vertex["x"], vertex["y"], vertex["z"]],
                             axis=-1).astype(np.float64)
        return positions, np.asarray(vertex["cluster_id"], dtype=np.int32)
    return None


def label_sites(summary, surface_cloud, site_points=None):
    """Adds 'surface', 'surface_id' and 'surface_share' to each cluster in
    summary (changed in place). Returns the number of labelled sites.

    The label is the surface that most of the site's points sit on:
      - With site_points (the Classify output's own points and their
        cluster_id), each point of the site gets the classification of
        the nearest Segment point within SURFACE_MATCH_DISTANCE.
      - Without site_points, the Segment points inside the site's box
        (centroid +/- half the extent, at least SITE_MIN_SIZE) are used.
    surface_share is the fraction of those points on that surface (0-1),
    so a site across a corner shows a low share."""
    from scipy.spatial import cKDTree

    positions, classification, names = surface_cloud
    clusters = summary.get("clusters") or []
    if not clusters or not len(positions):
        return 0

    tree = None
    if site_points is not None:
        site_pos, site_ids = site_points
        flagged = site_pos[site_ids >= 0]
        if len(flagged):
            low = flagged.min(axis=0) - SURFACE_MATCH_DISTANCE
            high = flagged.max(axis=0) + SURFACE_MATCH_DISTANCE
            near = np.all((positions >= low) & (positions <= high), axis=1)
            near_index = np.nonzero(near)[0]
            if len(near_index):
                tree = cKDTree(positions[near_index])

    n_labelled = 0
    for index, cluster in enumerate(clusters):
        cluster_id = int(cluster.get("cluster_id", index))
        if site_points is not None:
            points = site_points[0][site_points[1] == cluster_id]
            if tree is None or not len(points):
                continue
            distance, nearest = tree.query(points, k=1)
            matched = distance <= SURFACE_MATCH_DISTANCE
            values = classification[near_index[nearest[matched]]]
            total = len(points)
        else:
            centroid = np.asarray(cluster["centroid"], dtype=np.float64)
            half = np.maximum(np.asarray(cluster.get("extent", [0, 0, 0]), dtype=np.float64),
                              SITE_MIN_SIZE) / 2.0
            inside = np.all(np.abs(positions - centroid) <= half, axis=1)
            values = classification[inside]
            total = len(values)
        if not len(values):
            continue
        ids, counts = np.unique(values, return_counts=True)
        best = int(np.argmax(counts))
        surface_id = int(ids[best])
        cluster["surface_id"] = surface_id
        cluster["surface"] = names.get(surface_id, f"surface_{surface_id}")
        cluster["surface_share"] = float(counts[best]) / float(total)
        n_labelled += 1
    return n_labelled


def _site_color(max_magnitude, largest):
    """Pale orange (small change) to strong red (largest change in this
    file) - sites are ranked against each other, not against a fixed
    distance, the same idea as diverging_colormap()."""
    t = max(0.0, min(1.0, max_magnitude / largest)) if largest > 0 else 1.0
    return Gf.Vec3f(1.0, 0.75 * (1.0 - t), 0.2 * (1.0 - t))


def add_damage_sites(stage, prim_path, summary):
    """
    Adds one marker per damage site under prim_path:

        <prim_path>                 Xform, with delta:nFlagged / nConfirmed /
                                    nNoise / threshold / source attributes
          /Site_00                  Xform at the site centroid (meters)
            /Bounds                 Cube, scaled to the site extent
                                    (at least SITE_MIN_SIZE per axis)

    Each Site_NN stores its own numbers as custom attributes, which show
    in Omniverse's Property panel under 'Raw USD Properties':
        delta:clusterId, delta:pointCount (int)
        delta:centroid (double3, m), delta:extent (float3, m)
        delta:meanMagnitude, delta:maxMagnitude (float, m - absolute M3C2
        distance, so always >= 0; the sign is in ChangeHighlight's colours)
        delta:surface (string), delta:surfaceId (int), delta:surfaceShare
        (float, 0-1) - only when label_sites() found the site's surface.
        The site's display name then includes the surface, for example
        "Site 03 - wall_2".

    Returns the number of sites added.
    """
    clusters = summary.get("clusters") or []
    group = UsdGeom.Xform.Define(stage, prim_path).GetPrim()
    for key, attr, type_name in (("n_flagged", "delta:nFlagged", Sdf.ValueTypeNames.Int),
                                 ("n_confirmed", "delta:nConfirmed", Sdf.ValueTypeNames.Int),
                                 ("n_noise", "delta:nNoise", Sdf.ValueTypeNames.Int),
                                 ("threshold", "delta:threshold", Sdf.ValueTypeNames.Float),
                                 ("source", "delta:source", Sdf.ValueTypeNames.String),
                                 ("surface_source", "delta:surfaceSource",
                                  Sdf.ValueTypeNames.String)):
        if summary.get(key) is not None:
            group.CreateAttribute(attr, type_name).Set(summary[key])

    largest = max((float(c.get("max_magnitude", 0.0)) for c in clusters), default=0.0)
    for index, cluster in enumerate(clusters):
        cluster_id = int(cluster.get("cluster_id", index))
        centroid = [float(v) for v in cluster["centroid"]]
        extent = [float(v) for v in cluster.get("extent", [0.0, 0.0, 0.0])]
        size = [max(v, SITE_MIN_SIZE) for v in extent]

        site = UsdGeom.Xform.Define(stage, f"{prim_path}/Site_{cluster_id:02d}")
        UsdGeom.XformCommonAPI(site).SetTranslate(Gf.Vec3d(*centroid))
        prim = site.GetPrim()
        prim.CreateAttribute("delta:clusterId", Sdf.ValueTypeNames.Int).Set(cluster_id)
        prim.CreateAttribute("delta:pointCount", Sdf.ValueTypeNames.Int).Set(
            int(cluster.get("point_count", 0)))
        prim.CreateAttribute("delta:centroid", Sdf.ValueTypeNames.Double3).Set(
            Gf.Vec3d(*centroid))
        prim.CreateAttribute("delta:extent", Sdf.ValueTypeNames.Float3).Set(Gf.Vec3f(*extent))
        for key, attr in (("mean_magnitude", "delta:meanMagnitude"),
                          ("max_magnitude", "delta:maxMagnitude")):
            if cluster.get(key) is not None:
                prim.CreateAttribute(attr, Sdf.ValueTypeNames.Float).Set(float(cluster[key]))
        display_name = f"Site {cluster_id:02d}"
        if cluster.get("surface") is not None:
            prim.CreateAttribute("delta:surface", Sdf.ValueTypeNames.String).Set(
                str(cluster["surface"]))
            prim.CreateAttribute("delta:surfaceId", Sdf.ValueTypeNames.Int).Set(
                int(cluster["surface_id"]))
            prim.CreateAttribute("delta:surfaceShare", Sdf.ValueTypeNames.Float).Set(
                float(cluster["surface_share"]))
            display_name += f" - {cluster['surface']}"
        if hasattr(prim, "SetDisplayName"):  # USD 23.11 and newer
            prim.SetDisplayName(display_name)

        box = UsdGeom.Cube.Define(stage, f"{prim_path}/Site_{cluster_id:02d}/Bounds")
        box.CreateSizeAttr(1.0)
        UsdGeom.XformCommonAPI(box).SetScale(Gf.Vec3f(*size))
        box.CreateDisplayColorPrimvar(UsdGeom.Tokens.constant).Set(
            Vt.Vec3fArray([_site_color(float(cluster.get("max_magnitude", 0.0)), largest)]))
        box.CreateDisplayOpacityPrimvar(UsdGeom.Tokens.constant).Set(
            Vt.FloatArray([SITE_OPACITY]))
    return len(clusters)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline", required=True, help="Cleaned baseline .ply")
    parser.add_argument("--change", required=True, help="M3C2 change-highlight .ply")
    parser.add_argument("--detail", default=None,
                         help="Optional: extract_damage_detail.py output - real "
                              "comparison-cloud geometry near flagged locations, "
                              "added as a third layer (/World/Compartment/DamageDetail) "
                              "alongside the abstract magnitude-only ChangeHighlight.")
    parser.add_argument("--flagged", default=None,
                         help="Optional: a Stage 6 (Classify) output .ply. Adds "
                              "/World/Compartment/FlaggedPoints with only its flagged "
                              "points (cluster_id >= 0, else classified = 1). For a Classify "
                              "run with 'Keep all points', where Stage 7 meshes the whole "
                              "surface for --change.")
    parser.add_argument("--clusters", default=None,
                         help="Optional: m3c2_classify.py's <name>.clusters.json - adds one "
                              "marker per damage site (/World/Compartment/DamageSites), with "
                              "its point count and magnitudes as 'delta:' attributes.")
    parser.add_argument("--surfaces", default=None,
                         help="Optional, with --clusters: a Stage 4 (Segment) cloud "
                              "(<name>_classified.ply or <name>_envelope_filtered.ply) of "
                              "the diff's reference scan. Each damage site gets the surface "
                              "it sits on (wall_2, floor, ...), from the manifest.json "
                              "beside that file.")
    parser.add_argument("--output", required=True, help="Output .usd/.usda path")
    parser.add_argument("--usdz", action="store_true",
                         help="Also package the result as a .usdz (same name, .usdz extension) "
                              "- needed for most web/AR/mobile USD viewers.")
    parser.add_argument("--voxel-size", type=float, default=None,
                         help="Optional: downsamples point cloud layers (not meshes) via "
                              "voxel-grid binning before writing - reduces point count / "
                              "processing load in viewers like Isaac Sim. Keep this smaller "
                              "than the smallest feature you care about seeing - it removes "
                              "redundant near-duplicate points from overlapping scan passes, "
                              "not real detail, as long as the value is chosen sensibly. Off "
                              "by default (no downsampling).")
    args = parser.parse_args()

    summary = None
    if args.clusters:
        # Read before anything is written, so a bad file fails the run cleanly.
        try:
            summary = load_clusters(args.clusters)
        except ValueError as e:
            print(f"ERROR: {e}")
            return 1
    if args.surfaces:
        if summary is None:
            print("ERROR: --surfaces needs --clusters (the surfaces label the damage sites).")
            return 1
        print(f"Reading surfaces: {args.surfaces}")
        try:
            surface_cloud = load_surface_cloud(args.surfaces)
        except (ValueError, OSError) as e:
            print(f"ERROR: {e}")
            return 1
        site_points = find_site_points(args.clusters, summary)
        method = ("the Classify points" if site_points is not None
                  else "the site boxes (Classify output not found beside the summary)")
        n_labelled = label_sites(summary, surface_cloud, site_points)
        summary["surface_source"] = str(args.surfaces)
        print(f"  {n_labelled} of {len(summary['clusters'])} damage site(s) labelled, "
              f"from {method}.")
        for cluster in summary["clusters"]:
            if cluster.get("surface") is not None:
                print(f"    site {cluster.get('cluster_id')}: {cluster['surface']} "
                      f"({cluster['surface_share'] * 100:.0f}% of its points)")

    stage = Usd.Stage.CreateNew(args.output)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    # All positions are in metres. Without this, USD readers use their
    # default of 0.01 m per unit (centimetres).
    UsdGeom.SetStageMetersPerUnit(stage, UsdGeom.LinearUnits.meters)
    world = UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(world.GetPrim())
    UsdGeom.Xform.Define(stage, "/World/Compartment")

    print(f"Reading baseline: {args.baseline}")
    add_layer(stage, "/World/Compartment/Baseline", args.baseline,
              BASELINE_POINT_WIDTH, uniform_color=BASELINE_DISPLAY_COLOR,
              voxel_size=args.voxel_size)

    print(f"Reading change-highlight: {args.change}")
    add_layer(stage, "/World/Compartment/ChangeHighlight", args.change,
              CHANGE_POINT_WIDTH, warn_if_missing=True, fallback_color=FALLBACK_HIGHLIGHT_COLOR,
              voxel_size=args.voxel_size)

    if args.detail:
        print(f"Reading damage detail: {args.detail}")
        add_layer(stage, "/World/Compartment/DamageDetail", args.detail,
                  CHANGE_POINT_WIDTH, warn_if_missing=True, fallback_color=FALLBACK_HIGHLIGHT_COLOR,
                  voxel_size=args.voxel_size)

    if args.flagged:
        print(f"Reading flagged points: {args.flagged}")
        add_flagged_layer(stage, "/World/Compartment/FlaggedPoints", args.flagged,
                          voxel_size=args.voxel_size)

    if summary is not None:
        print(f"Reading damage sites: {args.clusters}")
        n_sites = add_damage_sites(stage, "/World/Compartment/DamageSites", summary)
        if n_sites:
            print(f"  {n_sites} damage site marker(s) added.")
        else:
            print("  NOTE: the cluster summary has no damage sites - the DamageSites group "
                  "is empty.")

    stage.GetRootLayer().Save()
    print(f"Saved USD scene to: {args.output}")

    if args.usdz:
        usdz_path = Path(args.output).with_suffix(".usdz")
        package_as_usdz(args.output, usdz_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
