#!/usr/bin/env bash
# Open a smooth scrcpy mirror of the (headless) emulator — lightweight, no heavy Qt
# window, so no GNOME "(Not Responding)" flicker. Drives mouse + keyboard over adb.
#
# session_env.sh imports the desktop DISPLAY/WAYLAND so this works even when launched
# from a bare tty / this Claude session.
#
#   ./scripts/view.sh                 # mirror emulator-5554
#   ./scripts/view.sh --max-fps 60    # pass extra scrcpy flags through
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
. "$HERE/env.sh"
. "$HERE/session_env.sh"

adb devices | grep -q "emulator-.*device" || { echo "no emulator running — ./scripts/start_emulator.sh --headless first"; exit 1; }

# --no-audio: the emulator's audio forwarding is flaky and not needed here.
# --max-fps 30: cap frames so the software-rendered device isn't pushed too hard.
exec scrcpy -s emulator-5554 --no-audio --max-fps 30 "$@"
