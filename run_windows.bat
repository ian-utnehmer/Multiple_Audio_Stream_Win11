@echo off
setlocal
cd /d "%~dp0"

if /i "%~1"=="--skip-driver-setup" goto python_setup

where pyw >nul 2>nul
if errorlevel 1 (
  echo Python window launcher "pyw" was not found.
  echo Install Python 3 from https://www.python.org/downloads/windows/ and enable "Add python.exe to PATH".
  pause
  exit /b 1
)

start "" pyw -3 "%CD%\launch_ui.py"
if errorlevel 1 (
  echo Failed to start the launcher UI.
  pause
  exit /b 1
)
exit /b 0

:python_setup
where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher "py" was not found.
  echo Install Python 3 from https://www.python.org/downloads/windows/ and enable "Add python.exe to PATH".
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  py -3 -m venv .venv
  if errorlevel 1 (
    echo Failed to create the virtual environment.
    pause
    exit /b 1
  )
)

echo Installing/updating dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Dependency installation failed.
  pause
  exit /b 1
)

echo Starting Dual Output Router...
start "" ".venv\Scripts\pythonw.exe" "%CD%\dual_output_router.py"
if errorlevel 1 (
  echo Failed to start Dual Output Router.
  pause
  exit /b 1
)

exit /b 0
