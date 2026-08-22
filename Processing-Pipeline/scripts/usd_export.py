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

Accepts either a plain point cloud or a mesh (auto-detected via presence
of a 'face' element in the PLY) for --baseline and --change independently
- so you can mix and match, e.g. a meshed baseline with a still-points
change-highlight, or both as meshes.

Requires:
    pip install usd-core plyfile numpy

Usage (matches what the pipeline applet's Stage 6 calls):
    python usd_export.py --baseline baseline.ply --change change.ply --output scene.usd
    python usd_export.py --baseline baseline.ply --change change.ply --output scene.usd --usdz

Confirmed working for point clouds: producing a valid, readable .usd
(checked via Usd.Stage.Open + ExportToString). Mesh support (UsdGeom.Mesh
instead of UsdGeom.Points) is new and hasn't been run against real data
yet. --usdz packaging is also unconfirmed - check the console output on
first use of either.
"""

import argparse
import sys
from pathlib import Path

import numpy as np
from plyfile import PlyData
from pxr import Sdf, Usd, UsdGeom, UsdUtils, Vt

BASELINE_DISPLAY_COLOR = (0.5, 0.5, 0.5)     # muted grey
BASELINE_POINT_WIDTH = 0.01                   # meters
CHANGE_POINT_WIDTH = 0.03                     # meters, larger so it stands out
FALLBACK_HIGHLIGHT_COLOR = (1.0, 0.6, 0.0)    # used only if no scalar field is found


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

    stage = Usd.Stage.CreateNew(args.output)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.Xform.Define(stage, "/World")
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

    stage.GetRootLayer().Save()
    print(f"Saved USD scene to: {args.output}")

    if args.usdz:
        usdz_path = Path(args.output).with_suffix(".usdz")
        package_as_usdz(args.output, usdz_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
