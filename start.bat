@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo === Local Futures Trader ===
echo Safe local startup: packages stay inside the project .venv
echo First launch opens the guided browser setup automatically.
echo.

set "BOOTSTRAP="

REM Ein bereits vorhandenes Projekt-.venv direkt nutzen — dann braucht start.bat
REM KEIN System-py/python. (Haeufige Falle: Python 3.12 ist installiert, aber
REM nicht als py/python im PATH auffindbar; das venv existiert trotzdem.) Der
REM System-Python-Bootstrap unten laeuft dann nur noch beim allerersten Start,
REM wenn das .venv erst angelegt werden muss.
if exist ".venv\Scripts\python.exe" (
  set "BOOTSTRAP=.venv\Scripts\python.exe"
  goto run
)

REM WICHTIG: "if errorlevel N" (statt "if %ERRORLEVEL%==0") verwenden — in
REM einem verschachtelten Klammerblock wuerde %ERRORLEVEL% beim Parsen des
REM GESAMTEN aeusseren if/else einmalig eingesetzt ("eingefroren") und damit
REM den Exit-Code des inneren "where python" ignorieren. "if errorlevel N"
REM liest den Exit-Code dagegen live zum Ausfuehrungszeitpunkt.
where py >nul 2>&1
if errorlevel 1 (
  where python >nul 2>&1
  if errorlevel 1 (
    echo Python was not found. Install Python 3.11+: https://www.python.org/downloads/
    pause
    exit /b 1
  ) else (
    REM Microsoft-Store-Python-Stub-Falle: "where python" findet den Stub,
    REM obwohl kein echtes Python installiert ist. Der Stub liefert keine
    REM normale "Python X.Y.Z"-Ausgabe auf --version, daher hier verifizieren
    REM statt dem where-Fund blind zu vertrauen.
    REM WICHTIG: strikt "^Python <Ziffer>" pruefen, NICHT nur die Teilzeichen-
    REM kette "Python " — die Stub-Meldung ("Python was not found; run without
    REM arguments to install from the Microsoft Store ...") enthaelt "Python "
    REM selbst und wuerde ein loses findstr faelschlich als echtes Python werten.
    set "PYVER="
    for /f "delims=" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
    echo !PYVER! | findstr /r /c:"^Python [0-9]" >nul
    if errorlevel 1 (
      echo Python was not found ^(the Microsoft Store placeholder was detected^).
      echo Install Python 3.11+ from https://www.python.org/downloads/
      echo Tip: Open Windows Settings -^> Apps -^> App execution aliases
      echo and disable the Store aliases for python.exe/python3.exe.
      pause
      exit /b 1
    ) else (
      set "BOOTSTRAP=python"
    )
  )
) else (
  set "BOOTSTRAP=py -3"
)

REM Bootstrap: nur launch.py starten. launch.py erzeugt .venv und installiert
REM ausschliesslich dorthin (python -m pip --require-virtualenv). launch.py
REM prueft zusaetzlich als Allererstes die Python-Version (>=3.11) selbst.

:run
if /I "%~1"=="setup" (
  !BOOTSTRAP! scripts\launch.py --setup %2 %3 %4
) else (
  !BOOTSTRAP! scripts\launch.py %*
)

set ERR=%ERRORLEVEL%
if not %ERR%==0 (
  echo.
  echo The launcher stopped with exit code %ERR%.
  echo Check the message above ^(Python version, .venv, pip, or network^).
  echo For an offline retry, use: start.bat --skip-install
  pause
)
exit /b %ERR%
