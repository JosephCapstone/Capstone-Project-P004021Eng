"""USD cluster discovery and temporary scene visibility, without Kit imports."""

from dataclasses import dataclass
import math
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, Vt


@dataclass(frozen=True)
class DamageCluster:
    path: str
    cluster_id: int
    centroid: tuple
    extent: tuple
    point_count: int
    mean_magnitude: object
    max_magnitude: object
    surface: str
    shape_paths: tuple


def read_clusters(stage):
    if stage is None:
        return []
    clusters = []
    for prim in stage.Traverse():
        attr = prim.GetAttribute("delta:clusterId")
        if not attr or not attr.HasAuthoredValueOpinion():
            continue
        centroid = prim.GetAttribute("delta:centroid").Get()
        if (
            centroid is None
            or len(centroid) != 3
            or not all(math.isfinite(v) for v in centroid)
        ):
            continue
        parent = prim.GetParent()
        matrix = (
            UsdGeom.Xformable(parent).ComputeLocalToWorldTransform(
                Usd.TimeCode.Default()
            )
            if parent.IsA(UsdGeom.Xformable)
            else Gf.Matrix4d(1)
        )
        world = matrix.Transform(Gf.Vec3d(*centroid))
        raw_extent = prim.GetAttribute("delta:extent").Get() or (0, 0, 0)
        # World-axis aligned dimensions, including parent scale/rotation.
        extent = tuple(
            sum(abs(matrix[j][i]) * abs(raw_extent[j]) for j in range(3))
            for i in range(3)
        )
        shapes = tuple(
            str(child.GetPath())
            for child in Usd.PrimRange(prim)
            if child != prim and (child.IsA(UsdGeom.Points) or child.IsA(UsdGeom.Mesh))
        )
        clusters.append(
            DamageCluster(
                str(prim.GetPath()),
                int(attr.Get()),
                tuple(world),
                extent,
                int(prim.GetAttribute("delta:pointCount").Get() or 0),
                prim.GetAttribute("delta:meanMagnitude").Get(),
                prim.GetAttribute("delta:maxMagnitude").Get(),
                str(prim.GetAttribute("delta:surface").Get() or ""),
                shapes,
            )
        )
    return sorted(clusters, key=lambda c: (c.cluster_id, c.path))


def scene_layers(stage):
    layers = {
        key: []
        for key in ("Baseline", "Inspection", "Damage points", "Damage mesh", "Bounds")
    }
    if stage is None:
        return layers
    for prim in stage.Traverse():
        if not prim.IsA(UsdGeom.Imageable):
            continue
        path = str(prim.GetPath())
        name = prim.GetName().lower()
        if name == "bounds" and "/DamageSites/" in path:
            layers["Bounds"].append(path)
        elif prim.IsA(UsdGeom.Points) or prim.IsA(UsdGeom.Mesh):
            if "baseline" in name:
                key = "Baseline"
            elif "inspection" in name or "comparison" in name:
                key = "Inspection"
            elif any(
                token in path.lower()
                for token in ("damage", "change", "flagged", "shape")
            ):
                key = "Damage mesh" if prim.IsA(UsdGeom.Mesh) else "Damage points"
            else:
                continue
            layers[key].append(path)
    return layers


class SceneVisibility:
    """Author only in the session layer; restore previous session opinions."""

    def __init__(self, stage):
        self.stage = stage
        self.original = {}

    def set(self, path, visible):
        prim = self.stage.GetPrimAtPath(path)
        if not prim or not prim.IsA(UsdGeom.Imageable):
            return
        attr = UsdGeom.Imageable(prim).GetVisibilityAttr()
        layer = self.stage.GetSessionLayer()
        if path not in self.original:
            spec = layer.GetAttributeAtPath(attr.GetPath())
            self.original[path] = (
                spec is not None and spec.HasInfo("default"),
                spec.GetInfo("default") if spec and spec.HasInfo("default") else None,
            )
        with Usd.EditContext(self.stage, layer):
            attr.Set(UsdGeom.Tokens.inherited if visible else UsdGeom.Tokens.invisible)

    def restore(self):
        if not self.original or not self.stage:
            self.original.clear()
            return
        with Usd.EditContext(self.stage, self.stage.GetSessionLayer()):
            for path, (authored, value) in self.original.items():
                prim = self.stage.GetPrimAtPath(path)
                if prim:
                    attr = UsdGeom.Imageable(prim).GetVisibilityAttr()
                    if authored:
                        attr.Set(value)
                    else:
                        attr.Clear()
        self.original.clear()


class InspectionSession:
    """Keep temporary inspection and renderer edits out of source layers."""

    def __init__(self, stage):
        self.stage = stage
        self.root = stage.GetRootLayer()
        self.root_editable = self.root.permissionToEdit
        self.locked = False
        self._ready_frames = 0
        self.edit_target = stage.GetEditTarget()
        stage.SetEditTarget(stage.GetSessionLayer())
        # Some viewport callbacks explicitly target the root layer even when
        # the edit target is the session. Enforce inspection-only access there.

    def lock_when_ready(self):
        # Hydra must finish creating its render product before the source is
        # locked. Fresh pipeline exports do not have a /Render scope yet.
        self._ready_frames += 1
        if self._ready_frames >= 3:
            self.root.SetPermissionToEdit(False)
            self.locked = True

    def restore(self):
        self.root.SetPermissionToEdit(self.root_editable)
        if self.stage:
            self.stage.SetEditTarget(self.edit_target)


class ClusterHighlight:
    """Fade site boxes while retaining the pipeline's exported category colours."""

    def __init__(self, stage):
        self.stage = stage
        self.original = {}
        self.previous = None
        self.styles = {
            "All": ((1.0, 0.38, 0.06), 0.18),
            "Selected": ((1.0, 0.38, 0.06), 0.38),
            "Other": ((1.0, 0.38, 0.06), 0.012),
        }
        self.materials = {}
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            for name, (color, opacity) in self.styles.items():
                path = "/World/__DELTA_InspectionStyles/" + name
                material = UsdShade.Material.Define(stage, path)
                reader = UsdShade.Shader.Define(stage, path + "/BoxColor")
                reader.CreateIdAttr("UsdPrimvarReader_float3")
                reader.CreateInput("varname", Sdf.ValueTypeNames.String).Set(
                    "displayColor"
                )
                reader.CreateInput("fallback", Sdf.ValueTypeNames.Float3).Set(
                    Gf.Vec3f(*color)
                )
                reader.CreateOutput("result", Sdf.ValueTypeNames.Float3)
                shader = UsdShade.Shader.Define(stage, path + "/Surface")
                shader.CreateIdAttr("UsdPreviewSurface")
                shader.CreateInput(
                    "diffuseColor", Sdf.ValueTypeNames.Color3f
                ).ConnectToSource(reader.ConnectableAPI(), "result")
                shader.CreateInput("opacity", Sdf.ValueTypeNames.Float).Set(opacity)
                shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.8)
                material.CreateSurfaceOutput().ConnectToSource(
                    shader.ConnectableAPI(), "surface"
                )
                self.materials[name] = material

    def apply(self, clusters, selected, show_outline=True):
        if not self.stage:
            return
        layer = self.stage.GetSessionLayer()
        with Usd.EditContext(self.stage, layer):
            for c in clusters:
                prim = self.stage.GetPrimAtPath(c.path + "/Bounds")
                if not prim:
                    continue
                path = str(prim.GetPath())
                style = (
                    "All"
                    if selected is None
                    else "Selected" if c.path == selected else "Other"
                )
                previous_style = (
                    "All"
                    if self.previous is None
                    else "Selected" if c.path == self.previous else "Other"
                )
                if path in self.original and style == previous_style:
                    continue
                if path not in self.original:
                    saved = {}
                    for name in (
                        "primvars:displayColor",
                        "primvars:displayOpacity",
                        "primvars:displayOpacity:indices",
                        "primvars:displayColor:indices",
                    ):
                        attr = prim.GetAttribute(name)
                        spec = (
                            layer.GetAttributeAtPath(attr.GetPath()) if attr else None
                        )
                        saved[name] = (
                            bool(spec and spec.HasInfo("default")),
                            (
                                spec.GetInfo("default")
                                if spec and spec.HasInfo("default")
                                else None
                            ),
                        )
                    rel = layer.GetRelationshipAtPath(
                        prim.GetPath().AppendProperty("material:binding")
                    )
                    saved["binding"] = (
                        bool(rel),
                        (
                            list(prim.GetRelationship("material:binding").GetTargets())
                            if rel
                            else []
                        ),
                    )
                    self.original[path] = saved
                color, opacity = self.styles[style]
                geometry = UsdGeom.Gprim(prim)
                opacity_primvar = geometry.CreateDisplayOpacityPrimvar(
                    UsdGeom.Tokens.constant
                )
                opacity_primvar.Set(Vt.FloatArray([opacity]))
                if opacity_primvar.IsIndexed():
                    opacity_primvar.BlockIndices()
                UsdShade.MaterialBindingAPI.Apply(prim).Bind(self.materials[style])
        self.previous = selected

    def restore(self):
        if not self.stage:
            self.original.clear()
            return
        with Usd.EditContext(self.stage, self.stage.GetSessionLayer()):
            for path, saved in self.original.items():
                prim = self.stage.GetPrimAtPath(path)
                if not prim:
                    continue
                for name in (
                    "primvars:displayColor",
                    "primvars:displayOpacity",
                    "primvars:displayOpacity:indices",
                    "primvars:displayColor:indices",
                ):
                    authored, value = saved[name]
                    attr = prim.GetAttribute(name)
                    if authored:
                        attr.Set(value)
                    else:
                        attr.Clear()
                authored, targets = saved["binding"]
                rel = prim.GetRelationship("material:binding")
                if authored:
                    rel.SetTargets(targets)
                elif rel:
                    rel.ClearTargets(True)
            self.stage.RemovePrim("/World/__DELTA_InspectionStyles")
        self.original.clear()
