#!/usr/bin/env bash
# Worker launcher for systemd. The worker drives the shared real Chrome over CDP
# (AUTH_MODE=cdp) for the photo-download + tagging step. Driving a GUI Chrome needs
# the graphical session's environment, which a system service does NOT inherit — so
# import it here (same approach as tesla-reconcile/cron_clean.sh), then exec the
# worker. If the shared Chrome is already running on the CDP port, the worker just
# attaches and these vars are merely a fallback for the launch path.
set -e
cd "$(dirname "$0")"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"

# Pull DISPLAY / WAYLAND_DISPLAY / XAUTHORITY / DBUS etc. from the live graphical
# user session (cron/systemd lack these; without them Chrome can't open its CDP port).
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

exec .venv/bin/python worker.py
