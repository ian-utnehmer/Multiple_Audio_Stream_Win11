@echo off
setlocal
cd /d "%~dp0"

echo Disabling Windows test-signing/debug boot flags...
echo Approve the Administrator prompt if Windows shows one.
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Disable-TestSigning.ps1"
if errorlevel 1 (
  echo.
  echo Failed to run Disable-TestSigning.ps1.
  pause
  exit /b 1
)

echo.
echo Restart Windows after this finishes.
pause
