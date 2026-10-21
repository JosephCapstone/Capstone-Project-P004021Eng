@echo off
setlocal
REM Uses SETUP_PROCESSING.bat's environment; DELTA_PIPELINE_PYTHON can override it.
if not defined DELTA_PIPELINE_PYTHON set "DELTA_PIPELINE_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%DELTA_PIPELINE_PYTHON%" (
    echo Run SETUP_PROCESSING.bat first.
    pause
    exit /b 1
)
"%DELTA_PIPELINE_PYTHON%" "%~dp0exts\delta.robot.ui\delta\robot\ui\pipeline_launcher.py"
set "DELTA_EXIT_CODE=%ERRORLEVEL%"
if not "%DELTA_EXIT_CODE%"=="0" pause
exit /b %DELTA_EXIT_CODE%
