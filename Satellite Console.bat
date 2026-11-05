@echo off
rem Satellite Console - detailed line-by-line log of the receiver, on every band (see app\console.py)
rem   Satellite Console.bat                    watch the tracker's receiver
rem   Satellite Console.bat --receive          receive from here, without the tracker
rem   Satellite Console.bat --band gnss1       choose the band (iridium orbcomm ham2m ham70 gnss1 glo1 meteor; or keys 1-7)
title Satellite Console
set "ROOT=%~dp0"
set "PY=%ROOT%python"
set "PATH=%PY%;%PY%\Library\mingw-w64\bin;%PY%\Library\usr\bin;%PY%\Library\bin;%PY%\Scripts;%PY%\bin;%PATH%"
set "PYTHONNOUSERSITE=1"
set "OPENBLAS_NUM_THREADS=1"
cd /d "%ROOT%app"
"%PY%\python.exe" "%ROOT%app\console.py" %*
pause
