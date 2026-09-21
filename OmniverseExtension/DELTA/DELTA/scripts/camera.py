import math
import omni.ext
import omni.kit.app
import omni.usd
import omni.kit.viewport.utility
from pxr import Gf, Sdf, UsdGeom, Usd


def createCamera(stage, path: str, position: tuple = (0.0, 0.0, 0.0), rotation: tuple = (0.0, 0.0, 0.0)):
    """Creates and transforms a UsdGeom.Camera prim on the stage

        Args:
            stage (Usd.Stage): USD stage where prim should be created
            path (str): scene graph path for camera prim (e.g. '/World/Camera1)
            postition (tuple): World coordinates (X, Y, Z), defaults to (0.0, 0.0, 0.0)
            rotation (tuple): Euler rotation angles (X, Y, Z), defaults to (0.0, 0.0, 0.0)
    """

    #Define camera prim on stage
    camera = UsdGeom.Camera.Define(stage,Sdf.Path(path))

    #Add transform ops
    translate_op = camera.AddTranslateOp(UsdGeom.XformOp.PrecisionDouble)
    rotate_op = camera.AddRotateXYZOp(UsdGeom.XformOp.PrecisionFloat)

    #Apply transformations
    translate_op.Set(Gf.Vec3d(*position))
    rotate_op.Set(Gf.Vec3f(*rotation))

    return camera

def setViewportCamera(cameraPath: str):
    """Sets active viewport's camera to specified camera prim

        Args:
            camerapath (str): scene graph path for camera prim (e.g. '/World/Camera1)
           
    """
    #Retrieve active viewport instance
    viewport_api = omni.kit.viewport.utility.get_active_viewport()

    if viewport_api:
        viewport_api.camera_path = cameraPath
    else:
        print("[Error] No active viewport found.")

#Initalize idleCamera on startup. 
#Tune target, height and radius to USD dimensions
class idleCamera: 
    """Creates and animates a turntable camera orbiting a focal target.

    Supports stages with either Y-up or Z-up coordinate conventions and drives
    animation per-frame through the Omniverse Kit update loop.
    """
    def __init__(
        self,
        prim_path: str = "/World/idleCamera",
        target: tuple[float, float, float] = (0.0, 0.0, 0.0),
        radius: float = 50.0,
        height: float= 10.0,
        speed: float = 0.1,
    ):
        """Initializes the orbit camera and binds it to the application update loop.

        Args:
            prim_path (str): Stage path where the camera prim will be defined.
            target (tuple[float, float, float]): (X, Y, Z) point the camera orbits and faces.
            radius (float): Horizontal distance from the target center.
            height (float): Vertical offset along the stage up-axis.
            speed (float): Angular orbital velocity in radians per second.
        """
        # Resolve active stage and scene conventions
        self.stage = omni.usd.get_context().get_stage()
        self.up_axis = UsdGeom.GetStageUpAxis(self.stage)

        # Orbit configuration parameters
        self.target = Gf.Vec3d(*target)
        self.radius = radius
        self.height = height
        self.speed = speed
        self.current_time = 0.0

        # Create camera prim and prepare a 4x4 matrix transform op
        camera = createCamera(self.stage, prim_path)
        self.xformable = UsdGeom.Xformable(camera.GetPrim())
        self.matrix_op = self.xformable.AddTransformOp()

        #Subscribe to application render/update loop
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
        """Computes the camera's local-to-world matrix `lead_time` seconds ahead.

        Args:
            lead_time (float): Offset in seconds relative to current elapsed time.

        Returns:
            Gf.Matrix4d: 4x4 camera transform matrix at the predicted timestamp.
        """
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
    """Manages smooth animated transitions between two cameras in an Omniverse stage.

    Creates an intermediate camera prim that blends position (linear interpolation)
    and orientation (spherical linear interpolation) across a specified duration
    using smooth-step easing, then hands active viewport control to the destination camera.
    """
    def __init__(
            self,
            primPath: str = "/World/TransitionCamera",
    ):
        """Initializes the transition camera and prepares its USD transform op.

        Args:
            prim_path (str): Stage path where the intermediate transition camera will be defined.
        """
        self._stage = omni.usd.get_context().get_stage()

        # Define the transition camera prim and allocate its local 4x4 transform operator
        camera = createCamera(self._stage, primPath)
        self._xformable = UsdGeom.Xformable(camera.GetPrim())
        self._matrix_transition = self._xformable.AddTransformOp()

        # State tracking variables
        self._progress = 0.0
        self._duration = 2.0
        self._is_transitioning = False
        self._subscription = None


    def initiateTransition(self, start_camera: str, end_camera: str, duration: float = 2.0, idleCamera_instance = None):
        """Starts an interpolated transition from a source camera to a destination camera.

        Args:
            start_camera (str): Scene graph path of the origin camera prim.
            end_camera (str): Scene graph path of the destination camera prim.
            duration (float, optional): Total travel time in seconds. Defaults to 2.0.
            idleCamera_instance (IdleCamera, optional): If the destination is an orbiting
                camera, provide its instance to intercept its predicted future position
                instead of its current static transform.
        """
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

        setViewportCamera("/World/TransitionCamera")

        self._subscription = (
            omni.kit.app.get_app()
            .get_update_event_stream()
            .create_subscription_to_pop(self.transition)
        )

    def smoothStep(self, t: float) -> float:
        """Standard cubic Hermite easing curve (3t^2 - 2t^3) for smooth acceleration and deceleration."""
        return t * t * (3.0 - 2.0 * t)

    def transition(self, e):
        """Per-frame update callback driving camera interpolation and final handoff."""
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
            setViewportCamera(self._end_camera)
            self._is_transitioning = False
            self._subscription = None

    def stop(self):
        """Immediately halts active transition and detaches from the update event stream."""
        self._subscription = None







