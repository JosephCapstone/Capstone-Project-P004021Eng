@echo off
setlocal
REM Python 3.12 is required. This creates the processing environment in this folder.
if not exist "%~dp0.venv\Scripts\python.exe" py -3.12 -m venv "%~dp0.venv"
if errorlevel 1 goto failed
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0pipeline\requirements.txt"
if errorlevel 1 goto failed
echo Processing setup complete. Launch DELTA_Ui_LAUNCHER.bat and click USD Builder.
pause
exit /b 0
:failed
echo Setup failed. Check Python 3.12 is installed and review the error above.
pause
exit /b 1
