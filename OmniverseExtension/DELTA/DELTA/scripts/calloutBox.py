import carb
import omni.kit.app
import omni.kit.viewport.utility as vp_utils
import omni.ui.scene as sc
import omni.ui as ui
from pxr import Usd, UsdGeom, Gf
from omni.physx import get_physx_scene_query_interface



class CalloutBox:
    
    def __init__(
        self, 
        prim_path: str,
        frame_name: str,
    ):
        carb.log_warn("CALLOUTBOX CLASS INSTANTIATED")
        self._rectangle = None
        self._label = None
        self._label_transform = None
        self._label_background = None
        self._leader_point_transform = None
        self._transform = None
        self._scene_view = None
        self._height = 0.5
        self._width = 0.75
        self._x = 0.0
        self._y = 0.0
        self._label_width = 0.3
        self._label_height = 0.04
        self._label_x = 0.0
        self._label_y = 0.0
        self._horizontal_shift = 0.0
        self._color = 0xFF0000FF
        

        
        self._pulse_max = 255
        self._pulse_min = 75
        self._pulse_direction = -1
        self._pulse_speed = 100
        self._pulse_value = 50
        
        self._selected = False
        
        
        self._stage = omni.usd.get_context().get_stage()
        self._prim = self._stage.GetPrimAtPath(prim_path)
        if self._prim.IsValid():
            carb.log_warn("PRIM VALID")
        else:
            carb.log_warn("PRIM INVALID")
            
        self._vp_window = vp_utils.get_active_viewport_window()
        self._viewport_api = self._vp_window.viewport_api
        self._frame = self._vp_window.get_frame(frame_name)
        
        
        bbox_cache = UsdGeom.BBoxCache(
            Usd.TimeCode.Default(),
            [UsdGeom.Tokens.default_]
        )
        
        self._world_bbox = bbox_cache.ComputeWorldBound(self._prim)
        
        self._aspect_ratio = 1/2
        
        self._create_ui()
        

        self._subscription = (
            omni.kit.app.get_app()
            .get_update_event_stream()
            .create_subscription_to_pop(
                self._on_update, name="_on_update"
            )
        )

        
        
        
        

        
    def _create_ui(self):
        with self._frame:
            self._scene_view = sc.SceneView()
            with self._scene_view.scene:
                self._transform = sc.Transform()
                with self._transform:
                    self._rectangle = sc.Rectangle(                        
                        self._width,
                        self._height,
                        color=self._color,
                        thickness=3.0,
                        fill=False,
                        wireframe=True,
                        visible=True,
                    )
                    

                    
                    self._label_transform = sc.Transform()
                    with self._label_transform:
                        self._label_background = sc.Rectangle(
                            width=self._label_width,
                            height=self._label_height,
                            color=self._color,
                            thickness=3.0,
                            fill=True,
                            wireframe=False,
                            visible=True,
                        )
                        self._leader_line = sc.Line(
                            [0.0, 0.0, 0.0],
                            [0.0, 0.0, 0.0],
                            color=self._color,
                            thickness=3.0,
                        )
                        self._label = sc.Label(
                            "damage point 1 damage point 1",
                            color=0xFF000000,
                            alignment=ui.Alignment.CENTER, 
                        )
                    
                    self._leader_point_transform = sc.Transform()
                    with self._leader_point_transform:
                        self._leader_point = sc.Arc(
                            radius=0.01,
                            begin=0.0,
                            end=360.0,
                            color=self._color,
                            sector=True,
                            wireframe=False,
                            visible=True,
                        )

                        
        #carb.log_warn("UI ELEMENT CREATED")
        
    def _set_position(self, x, y):
        position_matrix = sc.Matrix44.get_translation_matrix(x, y * self._aspect_ratio, 0.0)
        self._transform.transform = position_matrix
        
        #carb.log_warn("UI HAS BEEN MOVED")
    
    def _set_position_label(self, x, y):
        position_matrix = sc.Matrix44.get_translation_matrix(x, y * self._aspect_ratio, 0.0)
        self._label_transform.transform = position_matrix

    

    def _set_size(self, width, height):
        self._width = width + (width*0.2)
        self._height = (height * self._aspect_ratio) + (self._aspect_ratio * height*0.2)
        self._rectangle.width = self._width
        self._rectangle.height = self._height
        
        self._label_width = self._width
        
        self._label_background.width = max(self._label_width, 0.3)

    def _set_leader_line(self):
        start_x = 0.0
        start_y = -self._label_y/2 + self._height/2
        
        end_x = 0.0
        end_y = 0.0
        
        self._leader_line.start = (start_x, start_y, 0.0)
        self._leader_line.end = (end_x, end_y, 0.0)
        
    def _set_leader_point(self):
        self._leader_point_transform.transform = (
            sc.Matrix44.get_translation_matrix(
                0.0,
                self._height/2, 
                0.0
            )
        )
    
    def _get_label_x(self):
        return self._x
        
    def _set_visibility(self, visibility):
        self._rectangle.visible = visibility
        self._leader_point.visible = not visibility
        
    def _set_color(self, color):
        self._rectangle.color = color
        self._label_background.color = color
        self._leader_line.color = color
        self._leader_point.color = color
        
    
    def _set_selected(self, selected):
        self._selected = selected
    
    def _get_bounds(self):

        aligned_range = self._world_bbox.ComputeAlignedRange()
        #carb.log_warn(str(aligned_range))
        
        maximum = aligned_range.GetMax()
        minimum = aligned_range.GetMin()
        
        #carb.log_warn(f"Minimum: {minimum}")
        #carb.log_warn(f"Maximum: {maximum}")
        corners = []
        for x in [minimum[0], maximum[0]]:
            for y in [minimum[1], maximum[1]]:
                for z in [minimum[2], maximum[2]]:
                    corners.append(Gf.Vec3d(x, y, z))
        #carb.log_warn(f"Corners: {corners}")
        #carb.log_warn(f"Number of corners: {len(corners)}")
        return corners
    
    def _ndc_bounds(self, corners):
        ndc_corners = []
        world_to_ndc = self._viewport_api.world_to_ndc
        
        for corner in corners:
            ndc_corners.append(world_to_ndc.Transform(corner))
            
        return ndc_corners
    
    def _2d_bounds(self, ndc_corners):
        first_point = ndc_corners[0]

        furthest_points = [
            first_point[0],
            first_point[0],
            first_point[1],
            first_point[1],
        ]
        for point in ndc_corners:
            if point[0] < furthest_points[0]:
                furthest_points[0] = point[0]
                
            if point[0] > furthest_points[1]:
                furthest_points[1] = point[0]
                
            if point[1] < furthest_points[2]:
                furthest_points[2] = point[1]
                
            if point[1] > furthest_points[3]:
                furthest_points[3] = point[1]
                
        return furthest_points
    
    def _get_center(self, furthest_points):
        center_x = (furthest_points[0] + furthest_points[1])/2
        center_y = (furthest_points[2] + furthest_points[3])/2
        
        return center_x, center_y
    
    def _get_width_height(self, furthest_points):
        width = furthest_points[1] - furthest_points[0]
        height = furthest_points[3] - furthest_points[2]
        
        return width, height
    
    def _ndc_to_scene(self, x, y):
        aspect_ratio = 16 / 9

        scene_x = x * aspect_ratio
        scene_y = y

        return scene_x, scene_y
    
    def _get_target_center(self):
        aligned_range = self._world_bbox.ComputeAlignedRange()
        
        minimum = aligned_range.GetMin()
        maximum = aligned_range.GetMax()
        
        center = (minimum + maximum)/2
        
        return center
    
    def _get_camera_center(self):
        camera_path = self._viewport_api.camera_path
        
        camera_prim = self._stage.GetPrimAtPath(camera_path)
        camera_xform = UsdGeom.Xformable(camera_prim)
        world_transform = camera_xform.ComputeLocalToWorldTransform(
            Usd.TimeCode.Default()
        )
        camera_position = world_transform.ExtractTranslation()

        return camera_position
    
    def _check_camera_position(self, camera_position, target_position):
        origin = Gf.Vec3d(0.0, 0.0, 0.0)
        
        target_vector = target_position - origin
        camera_vector = camera_position - origin
        
        target_distance = target_vector.GetLength()
        camera_distance = camera_vector.GetLength()
        
        distance_valid = camera_distance > target_distance
        
        dot_product = Gf.Dot(target_vector, camera_vector)
        
        angle_valid = dot_product > 0
        
        position_valid = distance_valid and angle_valid

        
        return position_valid
    
    def _on_update(self, event):


        if self._transform is None:
            return
    
        corners = self._get_bounds()
        
        ndc_corners = self._ndc_bounds(corners)
        
        furthest_points = self._2d_bounds(ndc_corners)
        
        self._x, self._y = self._get_center(furthest_points)
        self._set_position(self._x, self._y)
        
        width, height = self._get_width_height(furthest_points)
        self._set_size(width, height)
    
        camera_position = self._get_camera_center()
        target_position = self._get_target_center()
        
        position_valid = self._check_camera_position(camera_position, target_position)

        if position_valid:
            self._set_visibility(True)
        else:
            self._set_visibility(False)
        
        if self._width >= 0.3:
            self._label_x = 0.0
            self._label_y = (self._height) + 0.04
        elif self._width < 0.3:
            self._label_x = 0.0 + self._horizontal_shift
            shrink_amount = (0.3 - self._width)/0.3
            rise_amount = (shrink_amount * 0.5)
            self._label_y = (self._height) + 0.04 + rise_amount
        
        self._set_position_label(self._label_x, self._label_y)
        self._set_leader_line()
        self._set_leader_point()
        
        if self._selected == True:
            dt = event.payload["dt"]
            
            self._pulse_value += (self._pulse_speed * dt * self._pulse_direction)
            
            if self._pulse_value >= self._pulse_max:
                self._pulse_value = self._pulse_max
                self._pulse_direction = -1
            elif self._pulse_value <= self._pulse_min:
                self._pulse_value = self._pulse_min
                self._pulse_direction = 1               
            
            brightness = self._pulse_value / 255.0
            color = (brightness, 0.0, 0.0, 1.0)
            self._set_color(color)
        else:
            self._set_color(0xFF0000FF)
            
        
        
    def destroy(self):
        pass