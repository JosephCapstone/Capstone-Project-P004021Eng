"""Preserve classified point membership and reconstructed mesh shapes per site."""
import numpy as np
from plyfile import PlyData
from pxr import Sdf, UsdGeom, Vt


def read_membership(path):
    ply = PlyData.read(str(path))
    vertex = ply["vertex"]
    if "cluster_id" not in (vertex.data.dtype.names or ()):
        return None
    values = np.asarray(vertex["cluster_id"], dtype=np.float64)
    if not np.all(np.isfinite(values)) or not np.allclose(values, np.rint(values), atol=1e-5, rtol=0):
        raise ValueError("cluster_id must contain finite integer IDs")
    positions = np.column_stack([vertex[k] for k in ("x", "y", "z")]).astype(np.float32)
    faces = None
    if "face" in ply and len(ply["face"]):
        faces = np.asarray([list(f) for f in ply["face"]["vertex_indices"]], dtype=np.int32)
        if faces.ndim != 2 or faces.shape[1] != 3:
            raise ValueError("Cluster mesh must be triangulated")
        if np.any(faces < 0) or np.any(faces >= len(positions)):
            raise ValueError("Mesh has invalid vertex indices")
    return positions, np.rint(values).astype(np.int32), faces


def add_cluster_shapes(stage, group_path, point_data=None, mesh_data=None):
    """Points are exact classified members. Mesh IDs are nearest-point labels.

    Mixed-label triangles are omitted rather than assigned to the wrong site.
    The existing centroid transform is retained; shape coordinates are local.
    """
    group = stage.GetPrimAtPath(group_path)
    if not group:
        return 0
    count = 0
    for site in group.GetChildren():
        cluster_id = site.GetAttribute("delta:clusterId").Get()
        if cluster_id is None:
            continue
        centroid = np.asarray(site.GetAttribute("delta:centroid").Get(), dtype=np.float32)
        added = False
        if point_data is not None:
            positions, ids = point_data[:2]
            points = np.asarray(positions)[np.asarray(ids) == cluster_id]
            if len(points):
                shape = UsdGeom.Points.Define(stage, str(site.GetPath()) + "/ShapePoints")
                shape.CreatePointsAttr(Vt.Vec3fArray.FromNumpy((points - centroid).astype(np.float32)))
                shape.CreateWidthsAttr(Vt.FloatArray([0.025]))
                shape.CreateVisibilityAttr(UsdGeom.Tokens.invisible)
                shape.CreateDisplayColorPrimvar(UsdGeom.Tokens.constant).Set(Vt.Vec3fArray([(1.0, 0.35, 0.05)]))
                shape.GetPrim().CreateAttribute("delta:shapeMethod", Sdf.ValueTypeNames.String).Set("classified cluster members")
                added = True
        if mesh_data is not None:
            positions, ids, faces = mesh_data
            if faces is not None and len(faces):
                selected = faces[np.all(ids[faces] == cluster_id, axis=1)]
                if len(selected):
                    used, inverse = np.unique(selected, return_inverse=True)
                    mesh = UsdGeom.Mesh.Define(stage, str(site.GetPath()) + "/ShapeMesh")
                    mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy((positions[used] - centroid).astype(np.float32)))
                    mesh.CreateFaceVertexCountsAttr(Vt.IntArray([3] * len(selected)))
                    mesh.CreateFaceVertexIndicesAttr(Vt.IntArray(inverse.ravel().tolist()))
                    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
                    mesh.CreateDoubleSidedAttr(True)
                    mesh.CreateVisibilityAttr(UsdGeom.Tokens.invisible)
                    mesh.CreateDisplayColorPrimvar(UsdGeom.Tokens.constant).Set(Vt.Vec3fArray([(1.0, 0.35, 0.05)]))
                    mesh.GetPrim().CreateAttribute("delta:shapeMethod", Sdf.ValueTypeNames.String).Set("Blender reconstruction; nearest-point cluster labels; mixed faces omitted")
                    added = True
        count += int(added)
    return count
