@echo off
rem Start SALU (desktop interface). Double-click or run from a terminal.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo SALU is not installed yet. See README.md - Setup.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m salu %*
