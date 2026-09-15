import math
import omni.ext
import omni.kit.app
import omni.usd
import omni.kit.viewport.utility
from pxr import Gf, Sdf, UsdGeom, Usd

def createCamera(stage, path: str, position: tuple = (0.0, 0.0, 0.0), rotation: tuple = (0.0, 0.0, 0.0)):
    camera = UsdGeom.Camera.Define(stage,Sdf.Path(path))

    translate_op = camera.AddTranslateOp(UsdGeom.XformOp.PrecisionDouble)
    rotate_op = camera.AddRotateXYZOp(UsdGeom.XformOp.PrecisionFloat)

    translate_op.Set(Gf.Vec3d(*position))
    rotate_op.Set(Gf.Vec3f(*rotation))

    return camera

def setViewport(cameraPath: str):
    viewport_api = omni.kit.viewport.utility.get_active_viewport()

    if viewport_api:
        viewport_api.camera_path = cameraPath
    else:
        print("[Error] No active viewport found.")

class idleCamera:
    def __init__(
        self,
        prim_path: str = "/World/idleCamera",
        target: tuple[float, float, float] = (0.0, 0.0, 0.0),
        radius: float = 50.0,
        height: float= 10.0,
        speed: float = 0.1,
    ):
        self.stage = omni.usd.get_context().get_stage()
        self.up_axis = UsdGeom.GetStageUpAxis(self.stage)
        self.target = Gf.Vec3d(*target)
        self.radius = radius
        self.height = height
        self.speed = speed
        self.current_time = 0.0

        camera = createCamera(self.stage, prim_path)
        self.xformable = UsdGeom.Xformable(camera.GetPrim())
        self.matrix_op = self.xformable.AddTransformOp()

        # 2. Subscribe to application render/update loop
        self._subscription = (
            omni.kit.app.get_app()
            .get_update_event_stream()
            .create_subscription_to_pop(self._on_update)
        )

    def _on_update(self, e):
        dt = e.payload["dt"]
        self.current_time += dt
        theta = self.current_time * self.speed

        # Compute position based on stage up-axis
        if self.up_axis == UsdGeom.Tokens.y:
            cam_pos = Gf.Vec3d(
                self.target[0] + self.radius * math.cos(theta),
                self.target[1] + self.height,
                self.target[2] + self.radius * math.sin(theta),
            )
            world_up = Gf.Vec3d(0.0, 1.0, 0.0)
        else:
            cam_pos = Gf.Vec3d(
                self.target[0] + self.radius * math.cos(theta),
                self.target[1] + self.radius * math.sin(theta),
                self.target[2] + self.height,
            )
            world_up = Gf.Vec3d(0.0, 0.0, 1.0)

        # Compute look-at matrix and set at default time
        view_matrix = Gf.Matrix4d().SetLookAt(cam_pos, self.target, world_up)
        self.matrix_op.Set(view_matrix.GetInverse())

    def get_predicted_transform(self, lead_time: float = 5.0) -> Gf.Matrix4d:
        """Calculates the camera's 4x4 transform matrix `lead_time` seconds in the future."""
        future_time = self.current_time + lead_time
        theta_future = future_time * self.speed

        if self.up_axis == UsdGeom.Tokens.y:
            cam_pos = Gf.Vec3d(
                self.target[0] + self.radius * math.cos(theta_future),
                self.target[1] + self.height,
                self.target[2] + self.radius * math.sin(theta_future),
            )
            world_up = Gf.Vec3d(0.0, 1.0, 0.0)
        else:
            cam_pos = Gf.Vec3d(
                self.target[0] + self.radius * math.cos(theta_future),
                self.target[1] + self.radius * math.sin(theta_future),
                self.target[2] + self.height,
            )
            world_up = Gf.Vec3d(0.0, 0.0, 1.0)

        # Compute camera transform (inverse of view matrix)
        view_matrix = Gf.Matrix4d().SetLookAt(cam_pos, self.target, world_up)
        return view_matrix.GetInverse()

    def stop(self):
        """Unsubscribe from the loop to stop camera motion."""
        self._subscription = None        


class transitionCamera:
    def __init__(
            self,
            primPath: str = "/World/TransitionCamera",
    ):
        self._stage = omni.usd.get_context().get_stage()
        camera = createCamera(self._stage, primPath)
        self._xformable = UsdGeom.Xformable(camera.GetPrim())
        self._matrix_transition = self._xformable.AddTransformOp()

        self._progress = 0.0
        self._duration = 2.0
        self._is_transitioning = False
        self._subscription = None


    def initiateTransition(self, start_camera: str, end_camera: str, duration: float = 2.0, idleCamera_instance = None):
        self._end_camera = end_camera
        prim_start = self._stage.GetPrimAtPath(start_camera)
        prim_end = self._stage.GetPrimAtPath(end_camera)

        if not prim_start.IsValid() or not prim_end.IsValid():
            print("[Error] one or more camera prims are invalid")
            return

        if idleCamera_instance == None:
            matrix_end: Gf.Matrix4d = UsdGeom.Xformable(prim_end).GetLocalTransformation()
        else:
            matrix_end: Gf.Matrix4d = idleCamera_instance.get_predicted_transform(lead_time=duration)

        matrix_start: Gf.Matrix4d = UsdGeom.Xformable(prim_start).GetLocalTransformation()

        self._start_pos = matrix_start.ExtractTranslation()
        self._start_quat = matrix_start.ExtractRotationQuat()

        self._end_pos = matrix_end.ExtractTranslation()
        self._end_quat = matrix_end.ExtractRotationQuat()

        self._duration = max(duration, 0.001)
        self._progress = 0.0
        self._is_transitioning = True

        setViewport("/World/TransitionCamera")

        self._subscription = (
            omni.kit.app.get_app()
            .get_update_event_stream()
            .create_subscription_to_pop(self.transition)
        )

    def smoothStep(self, t: float) -> float:
        return t * t * (3.0 - 2.0 * t)

    def transition(self, e):
        if not self._is_transitioning:
            self._subscription = None
            return
        dt = e.payload.get("dt", 0.0)
        self._progress += dt / self._duration

        t = min(max(self._progress, 0.0), 1.0)

        eased_t = self.smoothStep(t)

        current_pos = (1.0 - eased_t) * self._start_pos + eased_t * self._end_pos

        current_quat = (Gf.Slerp(eased_t, self._start_quat, self._end_quat))

        current_rot = Gf.Rotation(current_quat)

        composed_matrix = Gf.Matrix4d()
        composed_matrix.SetTransform(current_rot, current_pos)

        self._matrix_transition.Set(composed_matrix)

        if self._progress >= 1.0:
            setViewport(self._end_camera)
            self._is_transitioning = False
            self._subscription = None

    def stop(self):
        self._subscription = None







#  xform = UsdGeom.XformCommonAPI(cam_geom)
#  xform.SetRotate(Gf.Vec3f(0.0, 0.0, 0.0))
#  xform.SetTranslate(Gf.Vec3d(0.0, 0.0, 0.0))