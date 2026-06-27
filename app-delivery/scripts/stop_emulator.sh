#!/usr/bin/env bash
# Cleanly shut down the running app-delivery emulator (saves its snapshot so the
# next boot is fast and your app login persists).
#   ./scripts/stop_emulator.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
. "$HERE/env.sh"

if ! adb devices 2>/dev/null | grep -q "emulator-.*device"; then
  echo "no emulator running."; exit 0
fi
SERIAL="$(adb devices | awk '/emulator-.*device/{print $1; exit}')"
echo "stopping $SERIAL ..."
adb -s "$SERIAL" emu kill 2>/dev/null || true
# give it a moment to exit; fall back to killing the qemu process
for _ in 1 2 3 4 5; do adb devices | grep -q "emulator-.*device" || break; sleep 1; done
pkill -f "qemu-system-x86_64.* -avd $AVD_NAME" 2>/dev/null || true
echo "stopped."
