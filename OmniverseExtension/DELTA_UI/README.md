# DELTA version 05 — Isaac Sim

This build focuses on local Isaac Sim inspection and processing:

- Open USD scenes and navigate their primitives.
- Inspect damage points and move the viewport camera to them.
- Create baseline USD scenes from PLY point clouds.
- Align and compare baseline and inspection PLY files, then open the resulting USD.
- Choose Low, Medium, or High processing quality.

ROS 2, robot control, ROS topics, bag playback, and DB3 decoding are not part of this build. ROS bridge auto-start is explicitly disabled in `delta.robot.app.kit`.

Processing starts from the DELTA USD Builder inside Isaac Sim. Native point-cloud processing runs in a separate Python worker to avoid the startup crash caused by loading those dependencies into Kit. Results open automatically in Isaac Sim.

On Windows, set `ISAAC_SIM_ROOT` to the Isaac Sim 6.1 installation directory and run `DELTA_Ui_LAUNCHER.bat`.

Before using PLY processing, install the worker dependencies once with Isaac Sim's bundled Python (Python 3.12). From this directory in PowerShell:

```powershell
$env:ISAAC_SIM_ROOT = 'C:\isaacsim'
& "$env:ISAAC_SIM_ROOT\kit\python\python.exe" .\setup_worker.py
.\DELTA_Ui_LAUNCHER.bat
```

For Linux:

```bash
export ISAAC_SIM_ROOT=/full/path/to/isaac-sim
"$ISAAC_SIM_ROOT/python.sh" setup_worker.py
chmod +x DELTA_Ui_LAUNCHER.sh
./DELTA_Ui_LAUNCHER.sh
```

Dependencies install into the ignored `worker-deps` directory. They are loaded only by the background worker. Linux has not been tested for this version.
