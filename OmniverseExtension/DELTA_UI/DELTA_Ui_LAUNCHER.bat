@echo off
setlocal
if not defined DELTA_PIPELINE_PYTHON if exist "%~dp0.venv\Scripts\python.exe" set "DELTA_PIPELINE_PYTHON=%~dp0.venv\Scripts\python.exe"

if not defined ISAAC_SIM_ROOT if exist "C:\isaacsim\kit\kit.exe" set "ISAAC_SIM_ROOT=C:\isaacsim"
if not defined ISAAC_SIM_ROOT if exist "%USERPROFILE%\Downloads\isaac-sim-standalone-6.1.0-windows-x86_64\kit\kit.exe" set "ISAAC_SIM_ROOT=%USERPROFILE%\Downloads\isaac-sim-standalone-6.1.0-windows-x86_64"
set "KIT_EXECUTABLE=%ISAAC_SIM_ROOT%\kit\kit.exe"

if not exist "%KIT_EXECUTABLE%" (
    echo Isaac Sim 6.1 Kit executable not found: "%KIT_EXECUTABLE%"
    echo Set ISAAC_SIM_ROOT to the Isaac Sim 6.1 installation folder.
    exit /b 1
)

"%KIT_EXECUTABLE%" "%~dp0delta.robot.app.kit" ^
    --ext-folder "%ISAAC_SIM_ROOT%\apps" ^
    --ext-folder "%ISAAC_SIM_ROOT%\exts" ^
    --ext-folder "%ISAAC_SIM_ROOT%\extscache" ^
    --ext-folder "%ISAAC_SIM_ROOT%\extsUser" ^
    --ext-folder "%ISAAC_SIM_ROOT%\extsDeprecated" ^
    --ext-folder "%~dp0exts" %*

pause
