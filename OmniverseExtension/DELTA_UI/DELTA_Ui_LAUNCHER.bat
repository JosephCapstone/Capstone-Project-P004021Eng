@echo off
setlocal

rem Set ISAAC_SIM_ROOT externally to use a different Isaac Sim installation.
if not defined ISAAC_SIM_ROOT set "ISAAC_SIM_ROOT=C:\isaacsim"
set "KIT_EXECUTABLE=%ISAAC_SIM_ROOT%\kit\kit.exe"

if not exist "%KIT_EXECUTABLE%" (
    echo Isaac Sim 6.1 Kit executable not found: "%KIT_EXECUTABLE%"
    echo Set ISAAC_SIM_ROOT to the Isaac Sim 6.1 installation folder.
    exit /b 1
)

rem Open the DELTA experience directly; no ROS environment setup is needed.
"%KIT_EXECUTABLE%" "%~dp0delta.robot.app.kit" ^
    --ext-folder "%ISAAC_SIM_ROOT%\apps" ^
    --ext-folder "%ISAAC_SIM_ROOT%\exts" ^
    --ext-folder "%ISAAC_SIM_ROOT%\extscache" ^
    --ext-folder "%ISAAC_SIM_ROOT%\extsUser" ^
    --ext-folder "%ISAAC_SIM_ROOT%\extsDeprecated" ^
    --ext-folder "%~dp0exts" %*

pause
