@echo off
REM Launches the applet from gui\pipeline_applet.py, regardless of where
REM this .bat file is double-clicked from. %~dp0 is this .bat's own folder
REM (with a trailing backslash) - the applet's own SCRIPTS_DIR/CONFIGS_DIR
REM constants are similarly location-based (not working-directory-based),
REM so this launcher doesn't need to `cd` anywhere first.
python "%~dp0gui\pipeline_applet.py"
if errorlevel 1 pause
