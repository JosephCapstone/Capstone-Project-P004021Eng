"""Damage point callout and camera navigation."""

import omni.kit.app
import omni.usd
import omni.ui.scene as scene
import omni.ui as ui
from omni.kit.viewport.utility import get_active_viewport, get_active_viewport_window
from pxr import Gf, Sdf, Usd, UsdGeom
from .damage_summary import site_summary, card_position


# ---------- Viewport callout ----------


class ClusterBoxCallout:
    """Selected-box overlay adapted from JaysonBilek's 8cfd071 calloutBox.

    Reuses world-bound projection and label/leader-line tracking. Only one
    cluster is drawn, with actual box edges instead of a centroid rectangle.
    """
    EDGES = [(i,i^bit) for i in range(8) for bit in (1,2,4) if i<(i^bit)]

    def __init__(self, details_fn=None):
        self._details_fn=details_fn
        self._card=None
        self._summary=None
        self._corners=None
        self._text=""
        self._frame=None
        self._lines=[]
        self._label=None
        self._leader=None
        self._clip_plane=None
        self._subscription=omni.kit.app.get_app().get_update_event_stream().create_subscription_to_pop(
            self._on_update,name="DeltaSelectedClusterBox")

    def set_cluster(self,stage,path,label,visible=True):
        self._corners=None
        self._text=label
        self._summary=site_summary(stage,path) if stage and path else None
        if self._card is not None:
            self._card.visible=False
            self._card.rebuild()
        prim=stage.GetPrimAtPath(path+"/Bounds") if stage and path and visible else None
        if prim and prim.IsA(UsdGeom.Cube):
            half=float(UsdGeom.Cube(prim).GetSizeAttr().Get() or 1.0)/2
            matrix=UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
            self._corners=[matrix.Transform(Gf.Vec3d(half if i&1 else -half,half if i&2 else -half,half if i&4 else -half)) for i in range(8)]
        if self._corners is None:self._hide()

    def _ensure_overlay(self):
        if self._frame is not None:return True
        window=get_active_viewport_window()
        if not window:return False
        self._frame=window.get_frame("DeltaSelectedClusterBox")
        self._frame.clear()
        with self._frame:
            with ui.ZStack():
                self._scene_view=scene.SceneView(aspect_ratio_policy=scene.AspectRatioPolicy.STRETCH)
                with self._scene_view.scene:
                    self._lines=[scene.Line((0,0,0),(0,0,0),color=(1,.38,.06,1),thickness=2.5,visible=False) for _ in self.EDGES]
                    self._leader=scene.Line((0,0,0),(0,0,0),color=(1,.38,.06,1),thickness=2,visible=False)
                self._placer=ui.Placer(offset_x=12,offset_y=12)
                with self._placer:
                    self._card=ui.Frame(width=280,height=184,visible=False)
                    self._card.set_build_fn(self._build_card)
        return True

    def _build_card(self):
        if not self._summary:return
        with ui.ZStack():
            ui.Rectangle(style={"background_color":0xF525211C,"border_color":0xFF167BDF,"border_width":1,"border_radius":6})
            with ui.VStack(spacing=4, style={"margin":10}):
                ui.Label(self._text,height=24,elided_text=True,style={"font_size":17,"color":0xFF50B5FF})
                ui.Label("Severity: " + self._summary["severity"],height=22,elided_text=True)
                ui.Label("Size: " + self._summary["dimensions"],height=22,elided_text=True)
                ui.Label("Baseline: " + self._summary["baseline"],height=36,word_wrap=True)
                ui.Button("View details",height=28,clicked_fn=self._details_fn)

    def _hide(self):
        for line in self._lines:line.visible=False
        if self._leader:self._leader.visible=False
        if self._label:self._label.visible=False
        if self._card is not None:self._card.visible=False

    def set_clip_plane(self, equation):
        self._clip_plane=equation

    def _on_update(self,_event):
        if not self._corners or not self._ensure_overlay():return
        viewport=get_active_viewport()
        if not viewport:self._hide();return
        # Adaptation of Jayson's _ndc_bounds; STRETCH removes fixed aspect ratios.
        shown=[]
        for line,(i,j) in zip(self._lines,self.EDGES):
            a,b=self._corners[i],self._corners[j]
            if self._clip_plane is not None:
                normal=Gf.Vec3d(*self._clip_plane[:3])
                da=Gf.Dot(normal,a)+self._clip_plane[3]
                db=Gf.Dot(normal,b)+self._clip_plane[3]
                if da<0 and db<0:
                    line.visible=False
                    continue
                if (da<0)!=(db<0):
                    intersection=a+(b-a)*(da/(da-db))
                    if da<0:a=intersection
                    else:b=intersection
            projected=[viewport.world_to_ndc.Transform(corner) for corner in (a,b)]
            valid=all(viewport.view.Transform(corner)[2]<0 for corner in (a,b)) and all(abs(p[axis])<100 for p in projected for axis in (0,1))
            line.visible=valid
            if valid:
                line.start=(projected[0][0],projected[0][1],0)
                line.end=(projected[1][0],projected[1][1],0)
                shown.extend(projected)
        if not shown:self._hide();return
        min_x=min(p[0] for p in shown);max_x=max(p[0] for p in shown)
        min_y=min(p[1] for p in shown);max_y=max(p[1] for p in shown)
        if min_x>1 or max_x<-1 or min_y>1 or max_y<-1:self._hide();return
        width=float(self._frame.computed_width)
        height=float(self._frame.computed_height)
        if width <= 0 or height <= 0:self._hide();return
        x,y=card_position((min_x,min_y,max_x,max_y),(width,height),
                          (max(280,float(self._card.computed_width)),max(184,float(self._card.computed_height))))
        self._placer.offset_x=x
        self._placer.offset_y=y
        self._card.visible=True
        anchor_x=max(-1,min(1,(min_x+max_x)/2))
        anchor_y=max(-1,min(1,max_y))
        self._leader.start=(anchor_x,anchor_y,0)
        self._leader.end=(2*x/width-1,1-2*(y+50)/height,0)
        self._leader.visible=True

    def destroy(self):
        self._subscription=None
        if self._frame:self._frame.clear()
        self._frame=None
        self._card=None
        self._lines=[]
        self._label=None
        self._leader=None


# ---------- Camera navigation ----------


class PointCameraNavigator:
    """Damage point camera navigation."""

    CAMERA_PATH = Sdf.Path("/World/__DELTA_DamageViewCamera")

    def __init__(self):
        """Camera state setup."""
        self._subscription = None
        self._stage = None
        self._transform_op = None

    # Start moving the camera towards the selected damage point.
    def move_to(self, stage, target, distance, duration=1.2, direction=None):
        """Camera transition setup."""
        viewport = get_active_viewport()
        if stage is None or viewport is None:
            return False

        self.stop()
        target = Gf.Vec3d(*target)
        start_matrix = Gf.Matrix4d(viewport.transform)
        start_position = start_matrix.ExtractTranslation()
        direction = Gf.Vec3d(*direction) if direction is not None else start_position - target
        if direction.GetLength() < 1e-6:
            direction = Gf.Vec3d(1.0, -1.0, 1.0)
        direction.Normalize()

        up_axis = UsdGeom.GetStageUpAxis(stage)
        world_up = Gf.Vec3d(0.0, 1.0, 0.0) if up_axis == UsdGeom.Tokens.y else Gf.Vec3d(0.0, 0.0, 1.0)
        if abs(Gf.Dot(direction, world_up)) > 0.98:
            world_up = Gf.Vec3d(0.0, 0.0, -1.0) if up_axis == UsdGeom.Tokens.y else Gf.Vec3d(0.0, 1.0, 0.0)

        destination = Gf.Matrix4d(1.0)
        destination.SetLookAt(target + direction * distance, target, world_up)
        destination = destination.GetInverse()

        # Temporary session-layer camera
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            camera = UsdGeom.Camera.Define(stage, self.CAMERA_PATH)
            xform = UsdGeom.Xformable(camera.GetPrim())
            transform_ops = [
                op for op in xform.GetOrderedXformOps()
                if op.GetOpType() == UsdGeom.XformOp.TypeTransform
            ]
            self._transform_op = transform_ops[0] if transform_ops else xform.AddTransformOp()
            self._transform_op.Set(start_matrix)
            # Author the render-product camera relationship in the session too.
            # Hydra otherwise updates the relationship where it was authored in
            # a reopened scan's root layer, despite the current edit target.
            product_path = viewport.render_product_path
            product = stage.GetPrimAtPath(product_path) if product_path else None
            if product:
                product.CreateRelationship("camera").SetTargets([self.CAMERA_PATH])
            viewport.camera_path = self.CAMERA_PATH

        self._stage = stage
        self._start_position = start_position
        self._start_rotation = start_matrix.ExtractRotationQuat()
        self._end_position = destination.ExtractTranslation()
        self._end_rotation = destination.ExtractRotationQuat()
        self._elapsed = 0.0
        self._duration = max(float(duration), 0.001)
        self._subscription = omni.kit.app.get_app().get_update_event_stream().create_subscription_to_pop(
            self._on_update, name="DeltaDamageCameraMove"
        )
        return True

    def _on_update(self, event):
        """Camera transition update."""
        current = omni.usd.get_context().get_stage()
        if not self._stage or not current or current != self._stage:
            self.stop()
            return
        self._elapsed += event.payload.get("dt", 0.0)
        fraction = min(self._elapsed / self._duration, 1.0)
        eased = fraction * fraction * (3.0 - 2.0 * fraction)
        position = (1.0 - eased) * self._start_position + eased * self._end_position
        rotation = Gf.Rotation(Gf.Slerp(eased, self._start_rotation, self._end_rotation))
        transform = Gf.Matrix4d(1.0)
        transform.SetTransform(rotation, position)
        with Usd.EditContext(self._stage, self._stage.GetSessionLayer()):
            self._transform_op.Set(transform)
        if fraction >= 1.0:
            self.stop()

    def stop(self):
        """Camera transition cleanup."""
        self._subscription = None
        self._stage = None
        self._transform_op = None

    # Restore the original camera view.
    def reset_view(self):
        """Perspective camera reset."""
        self.stop()
        viewport = get_active_viewport()
        if viewport is not None and viewport.camera_path == self.CAMERA_PATH:
            stage = omni.usd.get_context().get_stage()
            if stage:
                with Usd.EditContext(stage, stage.GetSessionLayer()):
                    product_path = viewport.render_product_path
                    product = stage.GetPrimAtPath(product_path) if product_path else None
                    if product:
                        product.CreateRelationship("camera").SetTargets([Sdf.Path("/OmniverseKit_Persp")])
                    viewport.camera_path = Sdf.Path("/OmniverseKit_Persp")
