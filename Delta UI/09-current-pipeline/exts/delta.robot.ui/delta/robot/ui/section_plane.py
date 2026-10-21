"""Session-only mesh clipping and point display filtering; source layers stay intact."""

import math
import numpy as np
from pxr import Sdf, Usd, UsdGeom, UsdLux

PREFIX = "omni:rtx:scene:sectionPlane:"


def scene_bounds(stage):
    """World-space geometry bounds, independent of layer visibility and cameras."""
    cache = UsdGeom.BBoxCache(
        Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render],
        useExtentsHint=True,
        ignoreVisibility=True,
    )
    ranges = []
    for prim in stage.Traverse():
        if "__DELTA_" not in str(prim.GetPath()) and prim.IsA(UsdGeom.Boundable):
            bounds = cache.ComputeWorldBound(prim).ComputeAlignedRange()
            if not bounds.IsEmpty():
                ranges.append(bounds)
    if not ranges:
        return None
    low = tuple(min(r.GetMin()[i] for r in ranges) for i in range(3))
    high = tuple(max(r.GetMax()[i] for r in ranges) for i in range(3))
    return (low, high) if all(math.isfinite(v) for v in low + high) else None


class SectionPlane:
    def __init__(self, stage):
        # Kit exposes a weak stage proxy. Hold a native ref until session cleanup
        # finishes, including the CLOSING/CLOSED event pair.
        self._stage_cache = Usd.StageCache()
        self._stage_cache.Insert(stage)
        self.stage = stage
        # Keep the layer handle for cleanup after Kit releases its stage proxy.
        self._session_layer = stage.GetSessionLayer()
        self.bounds = scene_bounds(stage)
        self.axis = 1 if UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y else 2
        self.fraction = 0.5
        self.flipped = False
        self.enabled = False
        self._originals = {}
        self._backup = Sdf.Layer.CreateAnonymous("delta-slice-backup")
        self._bound_product = None
        self._point_clouds = None
        self._fill_path = None

    def _ensure_light(self):
        if self._fill_path or any(
            UsdLux.LightAPI(prim) for prim in self.stage.Traverse()
        ):
            return
        path = "/__DELTA_SliceFill"
        while self.stage.GetPrimAtPath(path):
            path += "_"
        self._fill_path = Sdf.Path(path)
        light = UsdLux.DomeLight.Define(self.stage, path)
        light.CreateIntensityAttr(500.0)
        light.CreateColorAttr((1.0, 1.0, 1.0))

    @property
    def position(self):
        if not self.bounds:
            return 0.0
        low, high = self.bounds
        return low[self.axis] + self.fraction * (high[self.axis] - low[self.axis])

    @property
    def equation(self):
        # RTX keeps the positive half-space. Default cuts off higher coordinates.
        normal = [0.0, 0.0, 0.0]
        normal[self.axis] = 1.0 if self.flipped else -1.0
        return normal + [-normal[self.axis] * self.position]

    def through(self, point):
        if self.bounds:
            low, high = self.bounds
            span = high[self.axis] - low[self.axis]
            self.fraction = (
                max(0.0, min(1.0, (point[self.axis] - low[self.axis]) / span))
                if span > 1e-9
                else 0.5
            )

    def _remember(self, prim, name):
        path = prim.GetPath().AppendProperty(name)
        layer = self._session_layer
        if path not in self._originals:
            exists = layer.GetPropertyAtPath(path) is not None
            self._originals[path] = exists
            if exists:
                Sdf.CreatePrimInLayer(self._backup, path.GetPrimPath())
                Sdf.CopySpec(layer, path, self._backup, path)

    def _write(self, prim, name, value, type_name):
        self._remember(prim, PREFIX + name)
        prim.CreateAttribute(PREFIX + name, type_name).Set(value)

    def _clip_points(self):
        # RTX's mesh section plane does not cull standard USD point spheres.
        # Filter their session display arrays together, preserving source values.
        if self._point_clouds is None:
            self._point_clouds = []
            for prim in self.stage.Traverse():
                if not prim.IsA(UsdGeom.Points) or "__DELTA_" in str(prim.GetPath()):
                    continue
                points = UsdGeom.Points(prim)
                source = points.GetPointsAttr().Get()
                if source is None or not len(source):
                    continue
                xyz = np.asarray(source)
                matrix = np.asarray(
                    points.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
                )
                arrays = [("points", source, 1)]
                for name in ("widths", "normals", "ids", "velocities", "accelerations"):
                    value = prim.GetAttribute(name).Get()
                    if value is not None and len(value) == len(xyz):
                        arrays.append((name, value, 1))
                for pv in UsdGeom.PrimvarsAPI(prim).GetPrimvars():
                    if pv.GetInterpolation() not in (
                        UsdGeom.Tokens.vertex,
                        UsdGeom.Tokens.varying,
                    ):
                        continue
                    attr = pv.GetIndicesAttr() if pv.IsIndexed() else pv.GetAttr()
                    if not attr.GetTypeName().isArray:
                        continue
                    value = attr.Get()
                    size = 1 if pv.IsIndexed() else pv.GetElementSize()
                    if value is not None and len(value) == len(xyz) * size:
                        arrays.append((attr.GetName(), value, size))
                self._point_clouds.append((prim, xyz, matrix, arrays, {}))
        for prim, xyz, matrix, arrays, coordinates in self._point_clouds:
            if self.axis not in coordinates:
                coordinates[self.axis] = (
                    xyz @ matrix[:3, self.axis] + matrix[3, self.axis]
                ).astype(np.float32)
            values = coordinates[self.axis]
            keep = values >= self.position if self.flipped else values <= self.position
            for name, value, size in arrays:
                data = np.asarray(value)
                if size == 1:
                    filtered = data[keep]
                else:
                    filtered = data.reshape((len(xyz), size) + data.shape[1:])[
                        keep
                    ].reshape((-1,) + data.shape[1:])
                value_type = type(value)
                result = (
                    value_type.FromNumpy(np.ascontiguousarray(filtered))
                    if hasattr(value_type, "FromNumpy")
                    else value_type(filtered.tolist())
                )
                self._remember(prim, name)
                prim.GetAttribute(name).Set(result)

    def apply(self, render_product_path):
        prim = (
            self.stage.GetPrimAtPath(render_product_path)
            if render_product_path
            else None
        )
        if not prim:
            return False
        with Usd.EditContext(self.stage, self.stage.GetSessionLayer()):
            # Render-product settings override Carb globals in Kit 109. Merely
            # changing /rtx/rendermode leaves a Minimal viewport unclipped.
            self._remember(prim, "omni:rtx:rendermode")
            prim.CreateAttribute("omni:rtx:rendermode", Sdf.ValueTypeNames.Token).Set(
                "RaytracedLighting"
            )
            self._write(prim, "plane", self.equation, Sdf.ValueTypeNames.FloatArray)
            self._write(prim, "sectionLight", False, Sdf.ValueTypeNames.Bool)
            self._write(
                prim,
                "enabled",
                bool(self.enabled and self.bounds),
                Sdf.ValueTypeNames.Bool,
            )
            if self.enabled and self.bounds:
                self._clip_points()
                self._ensure_light()
        self._bound_product = str(render_product_path)
        return True

    def restore(self):
        layer = self._session_layer
        if self._fill_path:
            spec = layer.GetPrimAtPath(self._fill_path)
            if spec:
                del layer.rootPrims[spec.name]
            self._fill_path = None
        for path, existed in self._originals.items():
            if existed:
                Sdf.CopySpec(self._backup, path, layer, path)
            else:
                spec = layer.GetPropertyAtPath(path)
                if spec:
                    spec.owner.RemoveProperty(spec)
        self._originals.clear()
        self._bound_product = None
        self.enabled = False
