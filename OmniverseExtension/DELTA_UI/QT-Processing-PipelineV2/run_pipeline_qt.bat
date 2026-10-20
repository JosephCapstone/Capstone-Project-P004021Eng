@echo off
REM Starts the D.E.L.T.A. Qt application (gui\pipeline_applet_qt_template.py).
REM %~dp0 is the folder of this .bat file, so you can start it from any
REM location. The application finds scripts\ and configs\ from its own
REM location, not from the working directory.
REM
REM The application compiles gui\pipeline_applet_qt_template.ui by itself
REM at startup when the compiled file is missing or older than the .ui file.
REM You do not need to run pyside6-uic.
REM
REM If the application stops with an error, this window stays open so you
REM can copy the error text.
python "%~dp0gui\pipeline_applet_qt_template.py"
if errorlevel 1 pause
