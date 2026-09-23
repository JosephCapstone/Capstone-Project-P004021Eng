#!/usr/bin/env python3
"""
surface_reconstruction.py
===========================
Converts a point cloud (baseline or classified change-highlight) into a
triangle mesh, for both visual quality (solid surfaces read much better
than raw points in Omniverse) and quantification (surface area always,
volume if the result is watertight).

Two methods:
  - poisson: interpolates one continuous smooth surface through the data.
    Works well for room shells, walls, floors, terrain. Tends to over-smooth
    complex/cluttered scenes (machinery, people, cables) into blobs, since
    it's trying to fit a single continuous surface through data that isn't
    actually one continuous surface.
  - ball_pivoting: "rolls" a ball of a given radius over the points and
    connects what it touches, staying much closer to the real point
    positions. Generally handles thin/complex/cluttered geometry better
    than Poisson, at the cost of more holes where point density is uneven.

Requires:
    pip install open3d numpy

Usage:
    python surface_reconstruction.py --input baseline.ply --output baseline_mesh.ply
    python surface_reconstruction.py --input baseline.ply --output baseline_mesh.ply --method ball_pivoting
    python surface_reconstruction.py --input classified.ply --output damage_mesh.ply --depth 8

STATUS: standalone script, not yet wired into the applet or USD export.
Both methods have been run successfully on real data. Poisson: worked
without errors on a dense, cluttered compartment scan (1M+ points), but
produced a "blobby" result there - the expected Poisson failure mode on
cluttered scenes, not a bug. Ball Pivoting: run on the same scan, kept all
original points as mesh vertices (no resampling) with a comparable surface
area to the Poisson result, as a sanity cross-check between the two. Which
method suits which content (e.g. Poisson for a cleaner room shell, Ball
Pivoting for cluttered/mechanical detail) is still an open question best
answered once a quieter baseline scan is available for comparison.

--carry-field is new and hasn't been run against real data yet.
"""

import argparse
import re
import sys

import numpy as np
import open3d as o3d
from plyfile import PlyData, PlyElement


def load_cloud(path):
    pcd = o3d.io.read_point_cloud(str(path))
    if len(pcd.points) == 0:
        raise ValueError(f"'{path}' loaded but has zero points.")
    return pcd


def ensure_normals(pcd, k_neighbors=30):
    """Poisson reconstruction requires oriented normals. Estimates them if
    missing, and tries to make their orientation consistent - Poisson
    produces inside-out or holey meshes if normals point in mixed
    directions, which is a common silent failure mode worth guarding against."""
    if not pcd.has_normals():
        print("  No normals present - estimating them (this can take a while on dense clouds).")
        pcd.estimate_normals(
            search_param=o3d.geometry.KDTreeSearchParamKNN(knn=k_neighbors))
    pcd.orient_normals_consistent_tangent_plane(k=k_neighbors)
    return pcd


def poisson_reconstruct(pcd, depth, density_trim_percentile):
    """
    Runs Poisson reconstruction, then optionally trims low-density
    vertices - Poisson extrapolates a watertight surface even where point
    coverage is thin, which shows up as spurious blobby geometry away from
    the real data. Trimming the lowest-density percentile removes most of
    that at the cost of the result no longer being watertight - this is a
    real tradeoff, not a free cleanup step, hence it being an argument
    rather than always-on.
    """
    mesh, densities = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(pcd, depth=depth)
    densities = np.asarray(densities)

    if density_trim_percentile > 0:
        threshold = np.percentile(densities, density_trim_percentile)
        low_density_vertices = densities < threshold
        n_removed = int(low_density_vertices.sum())
        mesh.remove_vertices_by_mask(low_density_vertices)
        print(f"  Trimmed {n_removed} low-density vertices "
              f"(below {density_trim_percentile}th percentile).")

    mesh.remove_degenerate_triangles()
    mesh.remove_duplicated_triangles()
    mesh.remove_duplicated_vertices()
    mesh.remove_non_manifold_edges()
    return mesh


def estimate_ball_radii(pcd, multipliers=(1.0, 2.0, 4.0)):
    """
    Ball Pivoting needs one or more ball radii, roughly matched to the
    point spacing - too small and the ball falls through gaps (holes
    everywhere), too large and it bridges gaps that shouldn't be bridged
    (over-smoothing, the exact problem we're trying to avoid). Estimates
    from the cloud's own average nearest-neighbor spacing rather than a
    fixed number, since that varies a lot between a dense static scan and
    a sparser handheld/mobile one. Multiple radii (small to large) let it
    fill in at more than one scale.
    """
    distances = pcd.compute_nearest_neighbor_distance()
    avg_spacing = float(np.mean(distances))
    return [avg_spacing * m for m in multipliers]


def ball_pivoting_reconstruct(pcd, radii):
    radii_vector = o3d.utility.DoubleVector(radii)
    mesh = o3d.geometry.TriangleMesh.create_from_point_cloud_ball_pivoting(pcd, radii_vector)
    mesh.remove_degenerate_triangles()
    mesh.remove_duplicated_triangles()
    mesh.remove_duplicated_vertices()
    mesh.remove_non_manifold_edges()
    return mesh


def report_metrics(mesh, method):
    n_vertices = len(mesh.vertices)
    n_triangles = len(mesh.triangles)
    print(f"  Vertices: {n_vertices}")
    print(f"  Triangles: {n_triangles}")

    if n_triangles == 0:
        print("  WARNING: mesh has no triangles - reconstruction likely failed. "
              "Check that the input cloud has enough points and reasonable density.")
        return

    area = mesh.get_surface_area()
    print(f"  Surface area: {area:.4f} (square meters, if input was in meters)")

    watertight = mesh.is_watertight()
    print(f"  Watertight: {watertight}")
    if watertight:
        volume = mesh.get_volume()
        print(f"  Volume: {volume:.6f} (cubic meters, if input was in meters)")
    else:
        suggestion = ("re-running with --density-trim-percentile 0 for a watertight "
                       "(but noisier) result" if method == "poisson" else
                       "trying --method poisson instead, which is more likely to "
                       "produce a watertight result on room-shell-like geometry")
        print(f"  Volume: not available - mesh is not watertight (common for partial "
              f"scans, damage regions, or cluttered/thin geometry). If you need a "
              f"volume estimate anyway, consider {suggestion}, or use a convex hull "
              f"as a rough upper-bound approximation.")


def load_original_field(path, field_name):
    """Reads --input via plyfile (not open3d, which drops custom scalar
    fields) to get the original per-point positions and a named field's
    values, for carrying that field through to the reconstructed mesh.

    Matches case/underscore/whitespace-insensitively before giving up:
    a PLY property name can't contain a literal space, so CloudCompare's
    on-screen field name "M3C2 distance" - the exact example this
    script's own --carry-field help text suggests - is stored on disk
    with underscores instead. m3c2_classify.py's find_distance_field()
    already had to solve this same on-disk-vs-displayed-name mismatch
    for the identical field; this reuses the same normalization (lower-
    case, underscores -> spaces) rather than requiring the caller to
    already know the exact literal on-disk spelling. Falls back to
    raising, listing available fields, only if no normalized match is
    found - or if more than one field normalizes to the same name,
    since guessing between two would risk the same silent-wrong-field
    mistake find_distance_field()'s own docstring describes hitting on
    a real run (thresholding CloudCompare's uncertainty field instead of
    the real distance field).
    """
    ply = PlyData.read(str(path))
    if "vertex" not in ply:
        raise ValueError(f"'{path}' has no vertex data.")
    vertex = ply["vertex"]
    available = list(vertex.data.dtype.names or ())

    def normalize(n):
        return n.lower().replace("_", " ").strip()

    if field_name in available:
        resolved_name = field_name
    else:
        target = normalize(field_name)
        matches = [n for n in available if normalize(n) == target]
        if len(matches) == 1:
            resolved_name = matches[0]
            print(f"  Note: '{field_name}' matched on-disk field '{resolved_name}' "
                  f"(case/underscore-insensitive match).")
        elif len(matches) > 1:
            raise ValueError(
                f"Field '{field_name}' matches more than one field in '{path}' once "
                f"case/spacing is ignored: {matches}. Pass one of these exact names "
                f"instead so the right one is unambiguous.")
        else:
            raise ValueError(f"Field '{field_name}' not found in '{path}'. "
                              f"Available fields: {available}")

    positions = np.stack([vertex["x"], vertex["y"], vertex["z"]], axis=-1).astype(np.float64)
    values = np.asarray(vertex[resolved_name], dtype=np.float64)
    return positions, values


def sanitize_ply_field_name(name):
    """
    A PLY property line is whitespace-delimited ("property float
    <name>"), so a literal space in a field name breaks the header
    format - it reads as an extra, unparseable token, not part of the
    name. This collapses whitespace to underscores before a --carry-field
    value is used as a NEW output property name in write_mesh(), so
    passing this script's own documented example verbatim
    (--carry-field "M3C2 distance") produces a valid file instead of a
    malformed one. Matches the underscore convention CloudCompare itself
    already uses on disk for the same field (see load_original_field()).
    """
    return re.sub(r"\s+", "_", name.strip())


def carry_field_to_mesh(mesh, original_positions, original_values):
    """
    Reconstruction (Poisson/Ball Pivoting) creates new/modified vertices
    that don't correspond 1:1 to the original points, so a field like M3C2
    distance can't just be copied across by index - this looks up each
    mesh vertex's nearest original point and uses that point's value
    instead. Approximate (nearest-neighbor, not interpolated), but the
    only practical option without reconstruction-aware field propagation.
    Requires scipy.
    """
    from scipy.spatial import cKDTree
    tree = cKDTree(original_positions)
    mesh_vertices = np.asarray(mesh.vertices)
    _, indices = tree.query(mesh_vertices, k=1)
    return original_values[indices]


def write_mesh(output_path, mesh, field_name=None, field_values=None):
    """
    Writes via plyfile rather than open3d.io.write_triangle_mesh, so an
    optional carried scalar field can be included as a named vertex
    property - open3d's mesh writer only supports positions/normals/colors,
    not arbitrary named fields.
    """
    vertices = np.asarray(mesh.vertices, dtype=np.float32)
    triangles = np.asarray(mesh.triangles, dtype=np.int32)

    if field_name and field_values is not None:
        vertex_dtype = [("x", "f4"), ("y", "f4"), ("z", "f4"), (field_name, "f4")]
        vertex_data = np.zeros(len(vertices), dtype=vertex_dtype)
        vertex_data["x"], vertex_data["y"], vertex_data["z"] = \
            vertices[:, 0], vertices[:, 1], vertices[:, 2]
        vertex_data[field_name] = field_values.astype(np.float32)
    else:
        vertex_dtype = [("x", "f4"), ("y", "f4"), ("z", "f4")]
        vertex_data = np.zeros(len(vertices), dtype=vertex_dtype)
        vertex_data["x"], vertex_data["y"], vertex_data["z"] = \
            vertices[:, 0], vertices[:, 1], vertices[:, 2]

    vertex_element = PlyElement.describe(vertex_data, "vertex")

    face_data = np.empty(len(triangles), dtype=[("vertex_indices", "i4", (3,))])
    face_data["vertex_indices"] = triangles
    face_element = PlyElement.describe(face_data, "face")

    PlyData([vertex_element, face_element], text=False).write(str(output_path))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, help="Input point cloud .ply")
    parser.add_argument("--output", required=True, help="Output mesh .ply")
    parser.add_argument("--method", choices=["poisson", "ball_pivoting"], default="poisson",
                         help="Reconstruction method. 'poisson' = smooth continuous "
                              "surface, good for room shells/walls/floors, tends to "
                              "over-smooth cluttered scenes. 'ball_pivoting' = stays "
                              "closer to the actual points, generally better for "
                              "machinery/cluttered/thin geometry. Default: poisson")
    parser.add_argument("--depth", type=int, default=9,
                         help="[poisson only] Octree depth - higher = more detail but "
                              "slower and more prone to noise/artifacts. 8-10 is a "
                              "reasonable range to start experimenting in. Default: 9")
    parser.add_argument("--density-trim-percentile", type=float, default=10,
                         help="[poisson only] Percentile of lowest-density vertices to "
                              "remove as likely reconstruction artifacts. 0 disables "
                              "trimming (keeps the mesh watertight). Default: 10")
    parser.add_argument("--ball-radii", default=None,
                         help="[ball_pivoting only] Comma-separated list of ball radii "
                              "in the same units as the point cloud, e.g. '0.02,0.04,0.08'. "
                              "If omitted, radii are auto-estimated from the cloud's own "
                              "point spacing.")
    parser.add_argument("--carry-field", default=None,
                         help="Name of a per-vertex scalar field in --input (e.g. 'M3C2 "
                              "distance') to carry through to the output mesh's vertices "
                              "via nearest-original-point lookup - so a change-highlight "
                              "mesh can still be colored by magnitude after reconstruction. "
                              "Requires scipy. Omit for the baseline mesh, which doesn't "
                              "need one.")
    args = parser.parse_args()

    print(f"Loading: {args.input}")
    pcd = load_cloud(args.input)
    print(f"  {len(pcd.points)} points loaded.")

    ensure_normals(pcd)

    if args.method == "poisson":
        print(f"Running Poisson reconstruction (depth={args.depth})...")
        mesh = poisson_reconstruct(pcd, args.depth, args.density_trim_percentile)
    else:
        if args.ball_radii:
            radii = [float(r) for r in args.ball_radii.split(",")]
            print(f"Running Ball Pivoting with manual radii: {radii}")
        else:
            radii = estimate_ball_radii(pcd)
            print(f"Running Ball Pivoting with auto-estimated radii: "
                  f"{[round(r, 5) for r in radii]}")
        mesh = ball_pivoting_reconstruct(pcd, radii)

    report_metrics(mesh, args.method)

    carried_values = None
    carried_field_name = None
    if args.carry_field:
        try:
            import scipy  # noqa: F401 (import check only)
        except ImportError:
            print("  WARNING: --carry-field requires scipy ('pip install scipy') - "
                  "skipping field carry-through, mesh will have no scalar field.")
        else:
            print(f"Carrying field '{args.carry_field}' onto the reconstructed mesh...")
            orig_positions, orig_values = load_original_field(args.input, args.carry_field)
            carried_values = carry_field_to_mesh(mesh, orig_positions, orig_values)
            carried_field_name = sanitize_ply_field_name(args.carry_field)
            print(f"  Carried to {len(carried_values)} mesh vertices "
                  f"(range: {carried_values.min():.4f} to {carried_values.max():.4f}).")

    write_mesh(args.output, mesh, carried_field_name, carried_values)
    print(f"Saved mesh to: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
