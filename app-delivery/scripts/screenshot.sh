#!/usr/bin/env bash
# Grab the emulator's current screen over adb (no GUI access needed) so we can see
# what you're seeing while you drive the app.
#
#   ./scripts/screenshot.sh            # -> screenshots/screen_<timestamp>.png (+ latest.png)
#   ./scripts/screenshot.sh foo.png    # -> screenshots/foo.png (+ latest.png)
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
. "$HERE/env.sh"

if ! adb devices 2>/dev/null | grep -q "emulator-.*device"; then
  echo "no emulator running — start it with ./scripts/start_emulator.sh" >&2
  exit 1
fi

DIR="$HERE/screenshots"; mkdir -p "$DIR"
NAME="${1:-screen_$(date +%Y%m%d_%H%M%S).png}"
OUT="$DIR/$NAME"

# exec-out streams raw bytes (no tty CRLF translation that would corrupt the PNG).
adb exec-out screencap -p > "$OUT"
cp -f "$OUT" "$DIR/latest.png"
echo "$OUT"
