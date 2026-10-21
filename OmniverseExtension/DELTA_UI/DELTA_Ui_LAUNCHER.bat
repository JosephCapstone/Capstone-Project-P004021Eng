@echo off
setlocal

REM CHANGE THIS LOCATION to the Isaac Sim folder containing kit\kit.exe.
set "ISAAC_SIM_LOCATION=C:\isaacsim"
REM ISAAC_SIM_ROOT, when set in PowerShell, overrides the location above.
if defined ISAAC_SIM_ROOT set "ISAAC_SIM_LOCATION=%ISAAC_SIM_ROOT%"
REM Also recognise the standard standalone download folder.
if not exist "%ISAAC_SIM_LOCATION%\kit\kit.exe" if exist "%USERPROFILE%\Downloads\isaac-sim-standalone-6.1.0-windows-x86_64\kit\kit.exe" set "ISAAC_SIM_LOCATION=%USERPROFILE%\Downloads\isaac-sim-standalone-6.1.0-windows-x86_64"

if not exist "%ISAAC_SIM_LOCATION%\kit\kit.exe" (
    echo Isaac Sim was not found. Edit ISAAC_SIM_LOCATION near the top of this file.
    pause
    exit /b 1
)

"%ISAAC_SIM_LOCATION%\kit\kit.exe" "%~dp0delta.robot.app.kit" ^
    --ext-folder "%ISAAC_SIM_LOCATION%\apps" ^
    --ext-folder "%ISAAC_SIM_LOCATION%\exts" ^
    --ext-folder "%ISAAC_SIM_LOCATION%\extscache" ^
    --ext-folder "%ISAAC_SIM_LOCATION%\extsUser" ^
    --ext-folder "%ISAAC_SIM_LOCATION%\extsDeprecated" ^
    --ext-folder "%~dp0exts" %*
set "DELTA_EXIT_CODE=%ERRORLEVEL%"
if not "%DELTA_EXIT_CODE%"=="0" pause
exit /b %DELTA_EXIT_CODE%
