@echo off
rem Iridium Console - detailed burst-by-burst view of the receiver (see app\console.py)
rem   Iridium Console.bat              watch the tracker's receiver
rem   Iridium Console.bat --receive    receive from here, without the tracker
title Iridium Console
set "ROOT=%~dp0"
set "PY=%ROOT%python"
set "PATH=%PY%;%PY%\Library\mingw-w64\bin;%PY%\Library\usr\bin;%PY%\Library\bin;%PY%\Scripts;%PY%\bin;%PATH%"
set "PYTHONNOUSERSITE=1"
set "OPENBLAS_NUM_THREADS=1"
cd /d "%ROOT%app"
"%PY%\python.exe" "%ROOT%app\console.py" %*
pause
