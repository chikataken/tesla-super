@echo off
REM Setup-once, run-every-time wrapper for tesla-reconcile (Windows).
REM   run.bat                       -> reconciliation (test_superdispatch.py)
REM   run.bat --count 200 --dry-run -> reconciliation with args
REM   run.bat login                 -> one-time login
REM   run.bat cleanup --apply       -> Tesla dashboard cleanup
REM   run.bat some_script.py [args] -> run any script in this folder
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

if "%~1"=="login" (
  shift
  python run_login.py %*
) else if "%~1"=="cleanup" (
  shift
  python tesla_cleanup.py %*
) else (
  echo %~1 | findstr /e ".py" >nul
  if not errorlevel 1 (
    set "SCRIPT=%~1"
    shift
    python %SCRIPT% %*
  ) else (
    python test_superdispatch.py %*
  )
)
