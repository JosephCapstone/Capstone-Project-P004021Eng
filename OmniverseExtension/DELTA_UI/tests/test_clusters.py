import sys
import unittest
from pathlib import Path
import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT / "exts/delta.robot.ui/delta/robot/ui"))
sys.path.insert(0,str(ROOT / "QT-Processing-PipelineV2/Scripts"))
from cluster_model import read_clusters, SceneVisibility, scene_layers, InspectionSession, ClusterHighlight
from cluster_geometry import add_cluster_shapes


def fixture():
    s=Usd.Stage.CreateInMemory()
    parent=UsdGeom.Xform.Define(s,"/World/DamageSites")
    UsdGeom.XformCommonAPI(parent).SetTranslate(Gf.Vec3d(100,0,0))
    site=UsdGeom.Xform.Define(s,"/World/DamageSites/Site_00")
    UsdGeom.XformCommonAPI(site).SetTranslate(Gf.Vec3d(10,20,30))
    p=site.GetPrim()
    p.CreateAttribute("delta:clusterId",Sdf.ValueTypeNames.Int).Set(0)
    p.CreateAttribute("delta:centroid",Sdf.ValueTypeNames.Double3).Set(Gf.Vec3d(10,20,30))
    p.CreateAttribute("delta:extent",Sdf.ValueTypeNames.Float3).Set(Gf.Vec3f(1,2,3))
    p.CreateAttribute("delta:pointCount",Sdf.ValueTypeNames.Int).Set(3)
    UsdGeom.Cube.Define(s,str(p.GetPath())+"/Bounds")
    return s,p


class ClusterTests(unittest.TestCase):
    def test_centroid_is_not_transformed_twice(self):
        s,p=fixture()
        c=read_clusters(s)[0]
        self.assertEqual(c.centroid,(110,20,30))
        self.assertEqual(c.extent,(1,2,3))
        self.assertEqual(c.shape_paths,())

    def test_inspection_edits_are_routed_to_session(self):
        s,p=fixture()
        original=s.GetRootLayer().ExportToString()
        previous=s.GetEditTarget().GetLayer()
        session=InspectionSession(s)
        self.assertEqual(s.GetEditTarget().GetLayer(),s.GetSessionLayer())
        v=SceneVisibility(s);v.set(str(p.GetPath()),False)
        self.assertEqual(UsdGeom.Imageable(p).ComputeVisibility(),"invisible")
        self.assertEqual(s.GetRootLayer().ExportToString(),original)
        v.restore();session.restore()
        self.assertEqual(s.GetEditTarget().GetLayer(),previous)
        self.assertEqual(read_clusters(s)[0].centroid,(110,20,30))

    def test_point_only_scene_is_not_a_cluster(self):
        s=Usd.Stage.CreateInMemory()
        UsdGeom.Points.Define(s,"/World/DamageDetail").CreatePointsAttr([(0,0,0)])
        self.assertEqual(read_clusters(s),[])

    def test_visibility_does_not_change_source_layer(self):
        s,p=fixture(); original=s.GetRootLayer().ExportToString()
        v=SceneVisibility(s);v.set(str(p.GetPath()),False)
        self.assertEqual(UsdGeom.Imageable(p).ComputeVisibility(),"invisible")
        self.assertEqual(s.GetRootLayer().ExportToString(),original)
        v.restore()
        self.assertEqual(UsdGeom.Imageable(p).ComputeVisibility(),"inherited")

    def test_visibility_restores_existing_session_opinion(self):
        s,p=fixture()
        with Usd.EditContext(s,s.GetSessionLayer()):
            UsdGeom.Imageable(p).GetVisibilityAttr().Set("invisible")
        v=SceneVisibility(s);v.set(str(p.GetPath()),True);v.restore()
        self.assertEqual(UsdGeom.Imageable(p).ComputeVisibility(),"invisible")

    def test_member_shape_uses_local_coordinates_and_excludes_mixed_faces(self):
        s,p=fixture()
        positions=np.array([[10,20,30],[11,20,30],[10,21,30],[50,50,50]],dtype=np.float32)
        ids=np.array([0,0,0,1])
        faces=np.array([[0,1,2],[1,2,3]])
        self.assertEqual(add_cluster_shapes(s,"/World/DamageSites",(positions,ids),(positions,ids,faces)),1)
        points=UsdGeom.Points(s.GetPrimAtPath(str(p.GetPath())+"/ShapePoints"))
        self.assertEqual(len(points.GetPointsAttr().Get()),3)
        self.assertEqual(tuple(points.GetPointsAttr().Get()[0]),(0,0,0))
        mesh=UsdGeom.Mesh(s.GetPrimAtPath(str(p.GetPath())+"/ShapeMesh"))
        self.assertEqual(len(mesh.GetFaceVertexCountsAttr().Get()),1)
        self.assertEqual(len(read_clusters(s)[0].shape_paths),2)
        self.assertEqual(len(scene_layers(s)["Damage mesh"]),1)

    def test_only_selected_box_is_highlighted_without_source_edits(self):
        path=Path.home()/"Downloads/qtv2test_export_002.usd"
        if not path.exists():self.skipTest("Local inspection sample unavailable")
        s=Usd.Stage.Open(str(path));original=s.GetRootLayer().ExportToString();cs=read_clusters(s)
        original_colors={c.path:s.GetPrimAtPath(c.path+"/Bounds").GetAttribute("primvars:displayColor").Get() for c in cs}
        styling=ClusterHighlight(s)
        styling.apply(cs,None)
        for c in cs:
            prim=s.GetPrimAtPath(c.path+"/Bounds")
            self.assertAlmostEqual(prim.GetAttribute("primvars:displayOpacity").Get()[0],.18)
            self.assertEqual(prim.GetAttribute("primvars:displayColor").Get(),original_colors[c.path])
        for index in (0,2):
            styling.apply(cs,cs[index].path)
            opacities=[s.GetPrimAtPath(c.path+"/Bounds").GetAttribute("primvars:displayOpacity").Get()[0] for c in cs]
            self.assertEqual(sum(v>.1 for v in opacities),1)
            self.assertAlmostEqual(opacities[index],.38)
            for i,c in enumerate(cs):
                color=s.GetPrimAtPath(c.path+"/Bounds").GetAttribute("primvars:displayColor").Get()
                self.assertEqual(color,original_colors[c.path])
                if i!=index:self.assertAlmostEqual(opacities[i],.012)
            self.assertEqual(s.GetRootLayer().ExportToString(),original)
        styling.apply(cs,None)
        self.assertTrue(all(abs(s.GetPrimAtPath(c.path+"/Bounds").GetAttribute("primvars:displayOpacity").Get()[0]-.18)<1e-6 for c in cs))
        styling.restore()
        self.assertAlmostEqual(s.GetPrimAtPath(cs[0].path+"/Bounds").GetAttribute("primvars:displayOpacity").Get()[0],.35)
        self.assertEqual(s.GetRootLayer().ExportToString(),original)

    def test_real_export_has_all_290_clusters(self):
        path=Path.home()/"Downloads/qtv2test_export_002.usd"
        if not path.exists(): self.skipTest("Local inspection sample not available")
        s=Usd.Stage.Open(str(path));c=read_clusters(s)
        self.assertEqual(len(c),290)
        self.assertEqual(sum(x.point_count for x in c),4065)
        self.assertAlmostEqual(c[0].centroid[0],-10.6142544746)


if __name__=="__main__": unittest.main()
