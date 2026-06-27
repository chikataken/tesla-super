#!/usr/bin/env bash
# One command to set up (first run) and launch the Shipment Creator website.
# First run creates the venv + installs deps; every run starts the app, brings up
# the public Cloudflare tunnel (https://shipments.wastake.com), and opens the local
# URL in your browser. The tunnel is stopped again when the app exits, so the two
# share a lifetime. Linux counterpart to web.bat.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "First run: creating .venv and installing dependencies..."
  # Plain `python3 -m venv` needs ensurepip, which the system Python may lack
  # (e.g. Debian/Ubuntu's python3.14 without python3-venv). Prefer uv, which
  # ships its own CPython; fall back to the stdlib venv where uv isn't present.
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python -r requirements.txt
  else
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
  fi
fi

# --- Cloudflare tunnel ---------------------------------------------------------
# Bring the public tunnel up alongside the app so the hostname is live whenever the
# app runs, and stop it again on exit (the trap below) so the two share a lifetime.
# Skipped gracefully if cloudflared or its config isn't present (runs local-only).
TUN_PID=""
POLLER_PID=""
CF_BIN="$(command -v cloudflared || echo "$HOME/.local/bin/cloudflared")"
CF_CONFIG="$HOME/.cloudflared/config.yml"
if [ -x "$CF_BIN" ] && [ -f "$CF_CONFIG" ]; then
  "$CF_BIN" tunnel --config "$CF_CONFIG" run >/tmp/sc-cloudflared.log 2>&1 &
  TUN_PID=$!
  echo "Cloudflare tunnel started (pid $TUN_PID) -> https://shipments.wastake.com"
else
  echo "cloudflared/config not found — running locally only (no public tunnel)."
fi

cleanup() {
  [ -n "$TUN_PID" ] && kill "$TUN_PID" 2>/dev/null || true
  [ -n "$POLLER_PID" ] && kill "$POLLER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Open the browser ourselves, reliably. app.py's built-in opener fires on a fixed
# 1.2s timer — which often races a cold-starting Chrome and lands on a blank window
# with the URL dropped. Instead, poll until the server actually answers (it writes
# instance.json with its real url), then open that. SC_OPEN_BROWSER stays off so we
# don't get a second, blank window.
(
  for _ in $(seq 1 60); do
    if [ -f instance.json ]; then
      url=$(.venv/bin/python -c "import json;print(json.load(open('instance.json'))['url'])" 2>/dev/null)
      if [ -n "$url" ] && curl -sf -o /dev/null "$url"; then
        { command -v xdg-open >/dev/null && xdg-open "$url"; } >/dev/null 2>&1 \
          || .venv/bin/python -c "import webbrowser;webbrowser.open('$url')" >/dev/null 2>&1
        echo "Opened $url"
        exit 0
      fi
    fi
    sleep 0.25
  done
  echo "Server didn't come up in time — open it manually: http://127.0.0.1:8000"
) &
POLLER_PID=$!

# App in the foreground (NOT exec — so the trap runs and stops the tunnel on exit).
.venv/bin/python app.py "$@"
