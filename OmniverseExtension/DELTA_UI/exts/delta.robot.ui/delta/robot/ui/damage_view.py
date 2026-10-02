"""Damage point callout and camera navigation."""

import omni.kit.app
import omni.ui.scene as scene
from omni.kit.viewport.utility import get_active_viewport, get_active_viewport_window
from pxr import Gf, Sdf, Usd, UsdGeom


class PointCallout:
    """Damage point screen marker."""

    def __init__(self):
        """Callout setup."""
        self._world_position = None
        self._frame = None
        self._scene_view = None
        self._rect = None
        self._transform = None
        self._subscription = omni.kit.app.get_app().get_update_event_stream().create_subscription_to_pop(
            self._on_update, name="DeltaDamagePointCallout"
        )

    def set_point(self, position):
        """Callout target update."""
        self._world_position = Gf.Vec3d(*position) if position is not None else None
        if self._world_position is None and self._rect is not None:
            self._rect.visible = False

    def _ensure_overlay(self):
        """Viewport overlay setup."""
        if self._frame is not None:
            return True

        window = get_active_viewport_window()
        if window is None:
            return False

        # Separate overlay frame
        self._frame = window.get_frame("DeltaDamagePointCallout")
        self._frame.clear()
        with self._frame:
            self._scene_view = scene.SceneView(aspect_ratio_policy=scene.AspectRatioPolicy.STRETCH)
            with self._scene_view.scene:
                self._transform = scene.Transform()
                with self._transform:
                    self._rect = scene.Rectangle(
                        width=0.12,
                        height=0.12,
                        color=0xFF0000FF,
                        thickness=3.0,
                        fill=False,
                        wireframe=True,
                    )
        self._rect.visible = False
        return True

    def _on_update(self, _event):
        """World-to-screen update."""
        if self._world_position is None or not self._ensure_overlay():
            return

        viewport = get_active_viewport()
        if viewport is None:
            self._rect.visible = False
            return

        eye_position = viewport.view.Transform(self._world_position)
        ndc_position = viewport.world_to_ndc.Transform(self._world_position)
        visible = (
            eye_position[2] < 0.0
            and abs(ndc_position[0]) <= 1.0
            and abs(ndc_position[1]) <= 1.0
        )
        self._rect.visible = visible
        if visible:
            self._transform.transform = scene.Matrix44.get_translation_matrix(
                ndc_position[0], ndc_position[1], 0.0
            )

    def destroy(self):
        """Callout cleanup."""
        self._subscription = None
        if self._frame is not None:
            self._frame.clear()
        self._frame = None
        self._scene_view = None
        self._rect = None
        self._transform = None


class PointCameraNavigator:
    """Damage point camera navigation."""

    CAMERA_PATH = Sdf.Path("/World/__DELTA_DamageViewCamera")

    def __init__(self):
        """Camera state setup."""
        self._subscription = None
        self._stage = None
        self._transform_op = None

    def move_to(self, stage, target, distance, duration=1.2):
        """Camera transition setup."""
        viewport = get_active_viewport()
        if stage is None or viewport is None:
            return False

        self.stop()
        target = Gf.Vec3d(*target)
        start_matrix = Gf.Matrix4d(viewport.transform)
        start_position = start_matrix.ExtractTranslation()
        direction = start_position - target
        if direction.GetLength() < 1e-6:
            direction = Gf.Vec3d(1.0, -1.0, 1.0)
        direction.Normalize()

        up_axis = UsdGeom.GetStageUpAxis(stage)
        world_up = Gf.Vec3d(0.0, 1.0, 0.0) if up_axis == UsdGeom.Tokens.y else Gf.Vec3d(0.0, 0.0, 1.0)
        if abs(Gf.Dot(direction, world_up)) > 0.98:
            direction = Gf.Vec3d(1.0, -1.0, 0.5)
            direction.Normalize()

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

        self._stage = stage
        self._start_position = start_position
        self._start_rotation = start_matrix.ExtractRotationQuat()
        self._end_position = destination.ExtractTranslation()
        self._end_rotation = destination.ExtractRotationQuat()
        self._elapsed = 0.0
        self._duration = max(float(duration), 0.001)
        viewport.camera_path = self.CAMERA_PATH
        self._subscription = omni.kit.app.get_app().get_update_event_stream().create_subscription_to_pop(
            self._on_update, name="DeltaDamageCameraMove"
        )
        return True

    def _on_update(self, event):
        """Camera transition update."""
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

    def reset_view(self):
        """Perspective camera reset."""
        self.stop()
        viewport = get_active_viewport()
        if viewport is not None and viewport.camera_path == self.CAMERA_PATH:
            viewport.camera_path = Sdf.Path("/OmniverseKit_Persp")
