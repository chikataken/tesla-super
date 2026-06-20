#!/usr/bin/env bash
# Run by cron daily at 09:00 local time (see `crontab -l`).
#
# The Tesla dashboard cleanup drives a REAL (off-screen) Chrome, so it needs the
# graphical session's environment — which cron does NOT provide. Set it explicitly,
# then run the cleanup with --apply and append everything to a log.
#
# Requirements at run time: the machine is on and this user is logged into the
# Wayland desktop, and the shared Chrome profile is still signed in to Tesla
# (re-run `./run.sh login` if a run logs that it hit a login page).
export HOME=/home/mbdtf
export XDG_RUNTIME_DIR=/run/user/1000
export PATH="/home/mbdtf/.local/bin:/usr/bin:/bin"

# Import the live graphical-session environment the systemd user manager holds —
# DISPLAY, WAYLAND_DISPLAY, XAUTHORITY (a per-login random path), XDG_SESSION_TYPE,
# DBUS, etc. Cron's environment lacks these, and without XAUTHORITY/XDG_SESSION_TYPE
# Chrome falls back to X11 and never opens its CDP port. This is the authoritative,
# re-login-proof source.
if command -v systemctl >/dev/null 2>&1; then
  while IFS= read -r line; do
    case "$line" in
      DISPLAY=*|WAYLAND_DISPLAY=*|XAUTHORITY=*|XDG_SESSION_TYPE=*|DBUS_SESSION_BUS_ADDRESS=*|XDG_DATA_DIRS=*)
        export "$line" ;;
    esac
  done < <(systemctl --user show-environment 2>/dev/null)
fi
# Fallbacks if systemctl didn't supply them.
: "${WAYLAND_DISPLAY:=wayland-0}"
: "${DISPLAY:=:0}"
: "${DBUS_SESSION_BUS_ADDRESS:=unix:path=/run/user/1000/bus}"
: "${XDG_SESSION_TYPE:=wayland}"
if [ -z "$XAUTHORITY" ]; then
  XAUTHORITY=$(ls -1 /run/user/1000/.mutter-Xwaylandauth.* 2>/dev/null | head -1)
fi
export WAYLAND_DISPLAY DISPLAY DBUS_SESSION_BUS_ADDRESS XDG_SESSION_TYPE XAUTHORITY

cd /home/mbdtf/projects/tesla-super/tesla-reconcile || exit 1
mkdir -p output/logs
LOG=output/logs/cron_clean.log
echo "===== cron clean START $(date) =====" >> "$LOG"
./clean.sh --apply >> "$LOG" 2>&1
echo "===== cron clean END (exit $?) $(date) =====" >> "$LOG"
