@echo off
setlocal
cd /d "%~dp0"

echo === Local Futures Trader ===
echo pip/Packages: NUR im Projekt-.venv  —  System-Python bleibt unangetastet
echo.

where py >nul 2>&1
if %ERRORLEVEL%==0 (
  set "BOOTSTRAP=py -3"
) else (
  where python >nul 2>&1
  if %ERRORLEVEL%==0 (
    set "BOOTSTRAP=python"
  ) else (
    echo Python nicht gefunden. Bitte Python 3.11+ installieren.
    pause
    exit /b 1
  )
)

REM Bootstrap: nur launch.py starten. launch.py erzeugt .venv und installiert
REM ausschließlich dorthin (python -m pip --require-virtualenv).

if /I "%~1"=="setup" (
  %BOOTSTRAP% scripts\launch.py --setup %2 %3 %4
) else (
  %BOOTSTRAP% scripts\launch.py %*
)

set ERR=%ERRORLEVEL%
if not %ERR%==0 (
  echo.
  echo Launcher beendet mit Code %ERR%
  pause
)
exit /b %ERR%
