@echo off
rem Windows launcher
setlocal

rem Isaac Sim path
if not defined ISAAC_SIM_ROOT set "ISAAC_SIM_ROOT=C:\isaacsim"
set "KIT_EXECUTABLE=%ISAAC_SIM_ROOT%\kit\kit.exe"

rem Isaac Sim installation check
if not exist "%KIT_EXECUTABLE%" (
    echo Isaac Sim 6.1.0 Kit executable not found: "%KIT_EXECUTABLE%"
    echo Set ISAAC_SIM_ROOT to your Isaac Sim 6.1.0 folder and try again.
    exit /b 1
)

rem DELTA launch command
"%KIT_EXECUTABLE%" "%~dp0delta.robot.app.kit" ^
    --ext-folder "%ISAAC_SIM_ROOT%\apps" ^
    --ext-folder "%ISAAC_SIM_ROOT%\exts" ^
    --ext-folder "%ISAAC_SIM_ROOT%\extscache" ^
    --ext-folder "%ISAAC_SIM_ROOT%\extsUser" ^
    --ext-folder "%ISAAC_SIM_ROOT%\extsDeprecated" ^
    --ext-folder "%~dp0exts" %*

rem Exit pause
pause
