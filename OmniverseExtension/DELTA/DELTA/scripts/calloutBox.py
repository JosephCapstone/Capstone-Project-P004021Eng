import carb
import omni.kit.app
import omni.kit.viewport.utility as vp_utils
import omni.ui.scene as sc
from pxr import Usd, UsdGeom, Gf


class CalloutBox:
    
    def __init__(
        self, 
        prim_path: str,
        frame_name: str,
    ):
        carb.log_warn("CALLOUTBOX CLASS INSTANTIATED")
        self._rectangle = None
        self._transform = None
        self._scene_view = None
        self._height = 0.5
        self._width = 0.75
        
        
        self._stage = omni.usd.get_context().get_stage()
        self._prim = self._stage.GetPrimAtPath(prim_path)
        if self._prim.IsValid():
            carb.log_warn("PRIM VALID")
        else:
            carb.log_warn("PRIM INVALID")
            
        self._vp_window = vp_utils.get_active_viewport_window()
        self._viewport_api = self._vp_window.viewport_api
        carb.log_warn(f"Viewport API: {self._viewport_api}")
        carb.log_warn(str(dir(self._viewport_api)))
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
                        color=0xFF0000FF,
                        thickness=3.0,
                        fill=False,
                        wireframe=True,
                        visible=True,
                    )
        #carb.log_warn("UI ELEMENT CREATED")
        
    def _set_position(self, x, y):
        position_matrix = sc.Matrix44.get_translation_matrix(x, y * self._aspect_ratio, 0.0)
        self._transform.transform = position_matrix
        
        #carb.log_warn("UI HAS BEEN MOVED")

    def _set_size(self, width, height):
        self._width = width + (width*0.2)
        self._height = (height * self._aspect_ratio) + (height*0.2)
        self._rectangle.width = self._width
        self._rectangle.height = self._height

    def _set_visibility(self, visibility):
        self._rectangle.visible = visibility
    
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
    
    def _on_update(self, event):

        if self._transform is None:
            return
    
        corners = self._get_bounds()
        
        ndc_corners = self._ndc_bounds(corners)
        
        furthest_points = self._2d_bounds(ndc_corners)
        
        center_x, center_y = self._get_center(furthest_points)
        self._set_position(center_x, center_y)
        
        width, height = self._get_width_height(furthest_points)
        self._set_size(width, height)
        
    def destroy(self):
        pass