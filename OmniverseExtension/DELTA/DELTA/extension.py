import omni.ext
import omni.ui as ui
import omni.kit.commands
import omni.usd
from pxr import Usd, Sdf
from .scripts.camera import createCamera, transitionCamera, idleCamera, setViewport
from.scripts.callout import CalloutBox



def create_reference(usd_context: omni.usd.UsdContext, path_to: Sdf.Path, asset_path: str, prim_path: Sdf.Path) -> Usd.Prim:
    omni.kit.commands.execute("CreateReference",
        usd_context=usd_context,
        path_to=path_to,
        asset_path=asset_path,
        prim_path=prim_path
    )
    return usd_context.get_stage().GetPrimAtPath(path_to) 

def importUSD ():
    context: omni.usd.UsdContext = omni.usd.get_context()

    ref_prim: Usd.Prim = create_reference(context, Sdf.Path("/world/ref_prim"), "G:/New folder/bunnyDouble.usd", Sdf.Path("/data3D"))

    stage: Usd.Stage = context.get_stage()
    usda = stage.GetRootLayer().ExportToString()
    print(usda)

    assert ref_prim.IsValid()

    assert ref_prim.GetPrimStack()[0].referenceList.prependedItems[0] == Sdf.Reference(assetPath="file:G:/New folder/bunnyDouble.usd", primPath=Sdf.Path("/data3D"))

    return



# Any class derived from `omni.ext.IExt` in top level module (defined in `python.modules` of `extension.toml`) will be
# instantiated when extension gets enabled and `on_startup(ext_id)` will be called. Later when extension gets disabled
# on_shutdown() is called.
class CompanyHelloWorld1Extension(omni.ext.IExt):
    # ext_id is current extension id. It can be used with extension manager to query additional information, like where
    # this extension is located on filesystem.
    def on_startup(self, ext_id):


        point1 = CalloutBox()

        
        # stage = omni.usd.get_context().get_stage()
        # transCamera = transitionCamera()
        


        # idle_cam = idleCamera(prim_path="/World/idleCamera", radius=50.0, height=10.0, speed=0.1,)
        # setViewport("/World/idleCamera")

        # self._DamageTab = ui.Window("Example Window", position_x=100, position_y=100, width=100, height=100, dock_preference=ui.DockPreference.RIGHT_BOTTOM)
        # with self._DamageTab.frame:
        #     with ui.VStack():               
        #         with ui.HStack():
        #             ui.Button("Camera1", clicked_fn=lambda: createCamera(stage, "/World/Camera1", position=(-1.0, -23.0, 13.0), rotation=(60.0, 0.0, 0.0)))
        #             ui.Button("Camera1", clicked_fn=lambda: createCamera(stage, "/World/Camera2", position=(16.0, 1, 4), rotation=(75.0, 0.0, 90.0)))
        #         with ui.HStack():
        #             ui.Button("Camera1 -> Camera2", clicked_fn=lambda: transCamera.initiateTransition("/World/Camera1", "/World/Camera2", duration = 5.0))
        #             ui.Button("Camera2 -> Camera1",  clicked_fn=lambda: transCamera.initiateTransition("/World/Camera2", "/World/Camera1"))
        #         with ui.HStack():
        #             ui.Button("Camera1 -> idleCamera",  clicked_fn=lambda: transCamera.initiateTransition("/World/Camera1", "/World/idleCamera", duration = 5.0, idleCamera_instance=idle_cam))
        #             ui.Button("IdleCamera -> Camera1",  clicked_fn=lambda: transCamera.initiateTransition("/World/idleCamera", "/World/Camera1", duration = 5.0))
        #         with ui.HStack():
        #             ui.Button("Camera2 -> idleCamera",  clicked_fn=lambda: transCamera.initiateTransition("/World/Camera2", "/World/idleCamera", duration = 5.0, idleCamera_instance=idle_cam))
        #             ui.Button("IdleCamera -> Camera2",  clicked_fn=lambda: transCamera.initiateTransition("/World/idleCamera", "/World/Camera2", duration = 5.0))







    def on_shutdown(self):
        print("[company.hello.world1] company hello world1 shutdown")
