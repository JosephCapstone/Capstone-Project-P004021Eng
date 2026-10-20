@echo off
setlocal
if not defined DELTA_PIPELINE_PYTHON if exist "%~dp0.venv\Scripts\python.exe" set "DELTA_PIPELINE_PYTHON=%~dp0.venv\Scripts\python.exe"
if not defined DELTA_PIPELINE_PYTHON set "DELTA_PIPELINE_PYTHON=%~dp0.venv\Scripts\python.exe"
"%DELTA_PIPELINE_PYTHON%" "%~dp0exts\delta.robot.ui\delta\robot\ui\pipeline_launcher.py"
if errorlevel 1 pause
