# DELTA

DELTA opens a USD inspection scene in Isaac Sim. It can select a `Points` prim,
show damage point coordinates, draw a red marker, and move the camera to the
selected point.

## Requirements

- NVIDIA Isaac Sim 6.1.0
- Windows 11 or supported Linux
- Supported NVIDIA GPU and driver

## Linux setup

### Default folder location

Put the extracted Isaac Sim and DELTA folders beside each other:

```text
parent-folder/
├── isaac-sim-standalone-6.1.0-linux-x86_64/
└── DELTA_UI/
```

Run the Isaac Sim post-install script once:

```bash
cd /full/path/to/isaac-sim-standalone-6.1.0-linux-x86_64
./post_install.sh
```

Make the DELTA launcher executable once:

```bash
cd "/full/path/to/DELTA_UI"
chmod +x DELTA_Ui_LAUNCHER.sh
```

Launch DELTA:

```bash
./DELTA_Ui_LAUNCHER.sh
```

### Different Isaac Sim location

Set `ISAAC_SIM_ROOT` to the folder containing `kit/kit`:

```bash
ISAAC_SIM_ROOT="/full/path/to/isaac-sim" ./DELTA_Ui_LAUNCHER.sh
```

To change the saved default path, edit this line in `DELTA_Ui_LAUNCHER.sh`:

```bash
DEFAULT_ISAAC_SIM_ROOT="/full/path/to/isaac-sim"
```

## Windows setup

### Default folder location

Extract Isaac Sim 6.1.0 here:

```text
C:\isaacsim
```

The executable should be located here:

```text
C:\isaacsim\kit\kit.exe
```

Run this file once:

```text
C:\isaacsim\post_install.bat
```

Double-click `DELTA_Ui_LAUNCHER.bat` to start DELTA.

### Different Isaac Sim location

Open Command Prompt in the DELTA folder and run:

```bat
set "ISAAC_SIM_ROOT=C:\full\path\to\isaac-sim"
DELTA_Ui_LAUNCHER.bat
```

To change the saved default path, edit this line in `DELTA_Ui_LAUNCHER.bat`:

```bat
if not defined ISAAC_SIM_ROOT set "ISAAC_SIM_ROOT=C:\full\path\to\isaac-sim"
```

`ISAAC_SIM_ROOT` must point to the folder containing `kit\kit.exe`.

## Using DELTA

1. Select **SELECT USD FILE**.
2. Open a `.usd`, `.usda`, or `.usdc` inspection file.
3. Select the `Points` prim containing the damage data.
4. Select one of the first five points.
5. Read the world coordinates in the side panel.
6. Select **VIEW DAMAGE POINT** to move the camera.

The red box follows the selected point. The temporary camera is stored in the
USD session layer and is not saved into the USD file.

## Project files

```text
DELTA_UI/
├── DELTA_Ui_LAUNCHER.bat
├── DELTA_Ui_LAUNCHER.sh
├── delta.robot.app.kit
└── exts/delta.robot.ui/
    ├── config/extension.toml
    └── delta/robot/ui/
        ├── __init__.py
        ├── extension.py
        └── damage_view.py
```

- `DELTA_Ui_LAUNCHER.bat`: Windows paths and launch command.
- `DELTA_Ui_LAUNCHER.sh`: Linux paths and launch command.
- `delta.robot.app.kit`: Application name and top-level dependencies.
- `extension.toml`: Extension details and required Isaac Sim services.
- `__init__.py`: Exposes the extension to Isaac Sim.
- `extension.py`: Side panel, USD loading, prim selection, and point details.
- `damage_view.py`: Red callout and camera transition.

## Main settings

The point preview limit is in `extension.py`:

```python
MAX_DAMAGE_POINTS = 5
```

The side panel size is set in `extension.py`:

```python
self._window = ui.Window(
    "DELTA",
    width=430,
    height=620,
)
```

The camera movement time is set in `damage_view.py`:

```python
def move_to(self, stage, target, distance, duration=1.2):
```
