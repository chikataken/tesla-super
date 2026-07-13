#!/usr/bin/env bash
# Launcher for the shared CDP Chrome (chrome-cdp.service user unit).
# Runs the long-lived automation Chrome on 127.0.0.1:9222 with the tesla-reconcile
# .auth profile — the logged-in profile every CDP tool attaches to (shipment-creator,
# terminals-sync, direct-pickup worker, cleaner flows).
#
# The window starts on the real session display, then is MINIMIZED via CDP once the
# debug port is up — same trick as auth.py's _place_window (--start-minimized doesn't
# stick at launch; a runtime minimize does, and the --disable-backgrounding/occlusion
# flags keep every tab live while minimized). If you restore the window, feel free to
# minimize it again — just don't CLOSE it (closing quits Chrome; systemd restarts it).
#
# Sessions: SuperDispatch re-login is automated (sd_login.py) — an expired SD session
# heals itself. Only a dead TESLA session needs a manual login:
#   systemctl --user stop chrome-cdp
#   /opt/google/chrome/chrome --user-data-dir=$HOME/projects/tesla-super/tesla-reconcile/.auth
#   ... log in to the Tesla portal, close Chrome ...
#   systemctl --user start chrome-cdp
set -e
cd "$(dirname "$0")"

# Graphical-session env (DISPLAY / WAYLAND_DISPLAY / XAUTHORITY / DBUS) so the real
# Chrome can open under systemd — same helper run.sh uses.
. "$(dirname "$0")/session_env.sh"

/opt/google/chrome/chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$PWD/.auth" \
  --window-size=1560,920 \
  --no-first-run \
  --no-default-browser-check \
  --disable-features=CalculateNativeWinOcclusion \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  --disable-background-timer-throttling &
CHROME_PID=$!

# Wait for the CDP port, then minimize the window(s). Best-effort: if it fails,
# Chrome still runs (just visible).
(
  for _ in $(seq 1 30); do
    curl -sf http://127.0.0.1:9222/json/version >/dev/null 2>&1 && break
    sleep 1
  done
  .venv/bin/python minimize_cdp_windows.py http://127.0.0.1:9222 || true
) &

# systemd (Type=simple) tracks this script; stay alive exactly as long as Chrome.
wait "$CHROME_PID"
