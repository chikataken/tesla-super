@echo off
REM One command to set up (first run) and run (every run) on Windows.
REM   run.bat --excel sheet.xlsx --sheet "VINs" --download-bols
REM   run.bat --headed              -> run with a visible, interactive browser
REM   run.bat login                 -> one-time visible login (save the session)
cd /d "%~dp0"

if not exist ".venv\" (
  echo First run: creating .venv and installing dependencies...
  python -m venv .venv
  call .venv\Scripts\activate.bat
  pip install -r requirements.txt
  python -m playwright install chromium
) else (
  call .venv\Scripts\activate.bat
)

if "%~1"=="login" (
  shift
  python run_login.py %*
) else (
  python main.py %*
)
