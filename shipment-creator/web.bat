@echo off
REM Start the Shipment Creator website. First run sets up the venv; every run
REM just launches it. Then open http://127.0.0.1:8000
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

start "" http://127.0.0.1:8000
python app.py
