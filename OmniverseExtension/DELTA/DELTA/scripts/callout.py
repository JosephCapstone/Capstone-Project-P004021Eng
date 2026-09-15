import omni.kit.app
import omni.kit.viewport.utility as vp_utils
import omni.ui.scene as scene
import omni.usd
from pxr import Usd, UsdGeom


class CalloutBox:

    def __init__(self):
        self._target_prim_path = "/World/Cube"
        self._setup_overlay()

        # Subscribe to update ticks only after UI is built
        self._subscription = (
            omni.kit.app.get_app()
            .get_update_event_stream()
            .create_subscription_to_pop(
                self._on_update, name="CalloutBoxUpdate"
            )
        )

    def _setup_overlay(self):
        vp_window = vp_utils.get_active_viewport_window()
        self._frame = vp_window.get_frame("ViewportOverlay")
        self._frame.clear()

        with self._frame:
            # Build scene graph once
            self._scene_view = scene.SceneView(
                aspect_ratio_policy=scene.AspectRatioPolicy.STRETCH
            )
            with self._scene_view.scene:
                # Retain reference to transform to update translation matrix dynamically
                self._transform = scene.Transform()
                with self._transform:
                    self._rect = scene.Rectangle(
                        width=0.25,
                        height=0.25,
                        color=0xFF0000FF,
                        thickness=3.0,
                        fill=False,
                        wireframe=True,
                    )

    def get_target_position(self):
        stage = omni.usd.get_context().get_stage()
        if not stage:
            return None, False

        vp_window = vp_utils.get_active_viewport_window()
        viewport_api = vp_window.viewport_api if vp_window else None
        cube_prim = stage.GetPrimAtPath(self._target_prim_path)

        if not viewport_api or not cube_prim.IsValid():
            return None, False

        time_code = Usd.TimeCode.Default()
        cube_xform = UsdGeom.Xformable(cube_prim)
        cube_world_pos = cube_xform.ComputeLocalToWorldTransform(
            time_code
        ).ExtractTranslation()

        cam_prim = stage.GetPrimAtPath(viewport_api.camera_path)
        if not cam_prim.IsValid():
            return None, False

        camera = UsdGeom.Camera(cam_prim)
        frustum = camera.GetCamera(time_code).frustum

        view_mat = frustum.ComputeViewMatrix()
        proj_mat = frustum.ComputeProjectionMatrix()

        # Eye space check: standard USD camera looks down -Z
        eye_pos = view_mat.Transform(cube_world_pos)
        is_behind = eye_pos[2] > 0.0

        # Transform World -> NDC
        ndc_pos = (view_mat * proj_mat).Transform(cube_world_pos)
        return ndc_pos, is_behind

    def _on_update(self, e):
        ndc_pos, is_behind = self.get_target_position()

        if ndc_pos is None or is_behind:
            self._rect.visible = False
            return

        self._rect.visible = True
        # Mutate the transform matrix in-place without rebuilding the widget tree
        self._transform.transform = scene.Matrix44.get_translation_matrix(
            ndc_pos[0], ndc_pos[1], 0.0
        )

    def destroy(self):
        self._subscription = None
        if self._frame:
            self._frame.clear()

