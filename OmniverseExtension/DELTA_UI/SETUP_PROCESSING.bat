@echo off
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" py -3.12 -m venv "%~dp0.venv"
if errorlevel 1 exit /b 1
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0QT-Processing-PipelineV2\requirements.txt"
if errorlevel 1 pause
