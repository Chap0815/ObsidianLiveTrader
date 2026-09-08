@echo off
setlocal
cd /d "%~dp0"
echo === Terminal Setup Assistant ===
echo Uses the project .venv when available and never installs globally.
echo.

set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if exist "%VENV_PY%" (
  echo Using project environment: %VENV_PY%
  "%VENV_PY%" scripts\setup_wizard.py --force
) else (
  echo No .venv yet. Starting the launcher to create it, then opening setup.
  if exist "%~dp0start.bat" (
    call "%~dp0start.bat" setup
  ) else (
    where py >nul 2>&1 && py -3 scripts\launch.py --setup || python scripts\launch.py --setup
  )
)

echo.
pause
