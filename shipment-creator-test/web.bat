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

REM --- Cloudflare tunnel ---------------------------------------------------
REM Bring the public tunnel up alongside the app. Locate cloudflared at its usual
REM install path, else fall back to PATH. The tunnel runs in its own window and is
REM stopped again when the app exits (taskkill below), so the two share a lifetime.
set "CLOUDFLARED=C:\cloudflared\cloudflared.exe"
if not exist "%CLOUDFLARED%" set "CLOUDFLARED=cloudflared"
set "CF_CONFIG=%USERPROFILE%\.cloudflared\config.yml"
if exist "%CF_CONFIG%" (
  start "Cloudflare Tunnel" "%CLOUDFLARED%" tunnel --config "%CF_CONFIG%" run
) else (
  start "Cloudflare Tunnel" "%CLOUDFLARED%" tunnel run
)

start "" http://127.0.0.1:8000
python app.py

REM App exited (Ctrl+C or window closed) -> stop the tunnel too so it isn't left
REM running orphaned in the background.
taskkill /im cloudflared.exe /f >nul 2>&1
