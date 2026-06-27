#!/usr/bin/env bash
# Daily terminal-sync launcher for systemd (terminals-sync.timer fires this at 13:30).
# Runs terminals_sync.py (push local edits to SuperDispatch, then pull the catalog back),
# which drives the shared real Chrome over CDP (AUTH_MODE=cdp). Driving a GUI Chrome needs
# the graphical session's environment, which a system service does NOT inherit — so import
# it here exactly like run_web.sh, then exec the sync.
set -e
cd "$(dirname "$0")"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"

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

exec .venv/bin/python terminals_sync.py
