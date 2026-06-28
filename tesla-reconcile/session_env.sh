# Ensure the graphical-session environment is present so the REAL Chrome can open.
#
# The tools drive a real (off-screen/visible) Chrome, which needs DISPLAY /
# WAYLAND_DISPLAY / XAUTHORITY etc. An interactive *desktop* terminal usually has
# these, but a plain tty, an SSH session, a tmux/screen started before login, or
# cron does NOT — and without them Chrome falls back to X11, finds no $DISPLAY,
# exits instantly, and the CDP port never opens ("Chrome did not open the CDP
# endpoint within 30s"). Import the live values from the systemd user manager,
# which is the authoritative, re-login-proof source.
#
# Source this (don't exec) early in a wrapper:  . "$(dirname "$0")/session_env.sh"
# Guarded: if a usable display is already in the environment, it leaves it alone.
if [ -z "$DISPLAY" ] && [ -z "$WAYLAND_DISPLAY" ]; then
  if command -v systemctl >/dev/null 2>&1; then
    while IFS= read -r _line; do
      case "$_line" in
        DISPLAY=*|WAYLAND_DISPLAY=*|XAUTHORITY=*|XDG_SESSION_TYPE=*|DBUS_SESSION_BUS_ADDRESS=*|XDG_RUNTIME_DIR=*|XDG_DATA_DIRS=*)
          export "$_line" ;;
      esac
    done < <(systemctl --user show-environment 2>/dev/null)
  fi
  # Fallbacks if the user manager didn't supply them.
  : "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
  : "${WAYLAND_DISPLAY:=wayland-0}"
  : "${DISPLAY:=:0}"
  : "${DBUS_SESSION_BUS_ADDRESS:=unix:path=${XDG_RUNTIME_DIR}/bus}"
  : "${XDG_SESSION_TYPE:=wayland}"
  if [ -z "$XAUTHORITY" ]; then
    XAUTHORITY=$(ls -1 "$XDG_RUNTIME_DIR"/.mutter-Xwaylandauth.* 2>/dev/null | head -1)
  fi
  export XDG_RUNTIME_DIR WAYLAND_DISPLAY DISPLAY DBUS_SESSION_BUS_ADDRESS XDG_SESSION_TYPE XAUTHORITY
fi
