@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo === Local Futures Trader ===
echo pip/Packages: NUR im Projekt-.venv  —  System-Python bleibt unangetastet
echo.

set "BOOTSTRAP="

REM WICHTIG: "if errorlevel N" (statt "if %ERRORLEVEL%==0") verwenden — in
REM einem verschachtelten Klammerblock wuerde %ERRORLEVEL% beim Parsen des
REM GESAMTEN aeusseren if/else einmalig eingesetzt ("eingefroren") und damit
REM den Exit-Code des inneren "where python" ignorieren. "if errorlevel N"
REM liest den Exit-Code dagegen live zum Ausfuehrungszeitpunkt.
where py >nul 2>&1
if errorlevel 1 (
  where python >nul 2>&1
  if errorlevel 1 (
    echo Python nicht gefunden. Bitte Python 3.11+ installieren: https://www.python.org/downloads/
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
      echo Python nicht gefunden ^(Microsoft-Store-Stub erkannt: "python" ist nur ein Platzhalter^).
      echo Bitte Python 3.11+ von https://www.python.org/downloads/ installieren.
      echo Tipp: In Windows unter "Einstellungen -^> Apps -^> App-Ausfuehrungsaliase"
      echo den Store-Alias fuer python.exe/python3.exe deaktivieren.
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

if /I "%~1"=="setup" (
  !BOOTSTRAP! scripts\launch.py --setup %2 %3 %4
) else (
  !BOOTSTRAP! scripts\launch.py %*
)

set ERR=%ERRORLEVEL%
if not %ERR%==0 (
  echo.
  echo Launcher beendet mit Code %ERR%
  echo Tipp: Meldung oben pruefen ^(Python-Version, venv, pip/Netzwerk^); bei
  echo Netzwerkproblemen "start.bat --skip-install" fuer Offline-Retry.
  pause
)
exit /b %ERR%
