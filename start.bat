@echo off
rem Start NyaySutra AI (opens http://127.0.0.1:8000 in your browser)
cd /d "%~dp0"
set "PY=%USERPROFILE%\.nyaysutra\venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Python environment not found at %PY%
  echo See README.md - Quick start.
  pause
  exit /b 1
)
"%PY%" run.py
pause
