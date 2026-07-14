#!/usr/bin/env bash
# Web-app launcher for systemd. app.py serves the site on 127.0.0.1:$PORT and spawns
# the pipeline (main.py --create), which drives the shared real Chrome over CDP
# (AUTH_MODE=cdp). Driving a GUI Chrome needs the graphical session's environment,
# which a system service does NOT inherit — so import it here (same approach as
# run_worker.sh), then exec the app. If the shared Chrome is already running on the
# CDP port the app just attaches and these vars are merely a fallback for launch.
set -e
cd "$(dirname "$0")"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"
export PORT="${PORT:-8001}"          # the cloudflared tunnel (test.wastake.com) targets :8001
export SC_TEST_MODE=1                 # READ-ONLY test build: all SuperDispatch writes blocked
# Read the production ledger. The test process never posts, so this is read-only in practice.
export SC_AUDIT_DB="${SC_AUDIT_DB:-$(cd ../shipment-creator && pwd)/posting_audit.db}"
# No SC_OPEN_BROWSER -> the server runs headless (never tries to open a desktop browser).

# Pull DISPLAY / WAYLAND_DISPLAY / XAUTHORITY / DBUS etc. from the live graphical user
# session (systemd lacks these; without them Chrome can't open its CDP port on a launch).
if command -v systemctl >/dev/null 2>&1; then
  while IFS= read -r line; do
    case "$line" in
      DISPLAY=*|WAYLAND_DISPLAY=*|XAUTHORITY=*|XDG_SESSION_TYPE=*|DBUS_SESSION_BUS_ADDRESS=*|XDG_DATA_DIRS=*)
        export "$line" ;;
    esac
  done < <(systemctl --user show-environment 2>/dev/null)
fi
: "${WAYLAND_DISPLAY:=wayland-0}"
: "${DISPLAY:=:0}"
: "${DBUS_SESSION_BUS_ADDRESS:=unix:path=/run/user/1000/bus}"
: "${XDG_SESSION_TYPE:=wayland}"
if [ -z "${XAUTHORITY:-}" ]; then
  XAUTHORITY=$(ls -1 /run/user/1000/.mutter-Xwaylandauth.* 2>/dev/null | head -1)
fi
export WAYLAND_DISPLAY DISPLAY DBUS_SESSION_BUS_ADDRESS XDG_SESSION_TYPE XAUTHORITY

exec .venv/bin/python app.py
