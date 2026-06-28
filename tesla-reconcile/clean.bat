@echo off
REM Portal cleaning — Tesla "Dispatch Dashboard 2.0" end-of-day cleanup.
REM Bumps every "ETA Today" / "Pickup Date Today" shipment to tomorrow.
REM
REM HEADLESS by default (a real Chrome parked off-screen — Tesla-safe). Add --headed
REM to watch it. DRY-RUN by default (counts + plan only); add --apply to submit.
REM   clean.bat                   -> dry-run, headless   (safe: shows the plan, changes nothing)
REM   clean.bat --apply           -> apply,   headless
REM   clean.bat --headed          -> dry-run, visible window
REM   clean.bat --apply --headed  -> apply,   visible window
REM
REM First-time login is shared with the rest of tesla-reconcile — if it lands on a
REM login page, run `run.bat login` once, then re-run clean.bat.
setlocal
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

python tesla_cleanup.py %*
