@echo off
rem Satellite Tracker - starts the app with the Python bundled in this folder
set "ROOT=%~dp0"
set "PY=%ROOT%python"
set "PATH=%PY%;%PY%\Library\mingw-w64\bin;%PY%\Library\usr\bin;%PY%\Library\bin;%PY%\Scripts;%PY%\bin;%PATH%"
set "QT_PLUGIN_PATH=%PY%\Library\plugins"
set "PYTHONNOUSERSITE=1"
cd /d "%ROOT%app"
start "" "%PY%\pythonw.exe" "%ROOT%app\start.py"
