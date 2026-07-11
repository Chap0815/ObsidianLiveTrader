@echo off
setlocal
cd /d "%~dp0"
echo === Einrichtungsassistent ===
echo Laeuft im Projekt-.venv falls vorhanden, sonst einmalig via System-Python
echo (kein pip an der Hauptinstallation).
echo.

set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if exist "%VENV_PY%" (
  echo Nutze venv: %VENV_PY%
  "%VENV_PY%" scripts\setup_wizard.py --force
) else (
  echo Noch kein .venv — starte Launcher mit --setup (erzeugt venv, dann Assistent)
  if exist "%~dp0start.bat" (
    call "%~dp0start.bat" setup
  ) else (
    where py >nul 2>&1 && py -3 scripts\launch.py --setup || python scripts\launch.py --setup
  )
)

echo.
pause
