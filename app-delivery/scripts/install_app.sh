#!/usr/bin/env bash
# Install the carrier app on the x86_64 emulator and make it actually run.
#
# The app (com.tesla.logisticsmobile) is React Native + arm64-only with
# extractNativeLibs=false. On the x86_64 emulator its ARM libs are translated fine,
# but the installer leaves the app's lib/arm64 dir EMPTY and SoLoader then searches
# for libs by the process ABI (x86_64) — which the APK doesn't ship — and crashes
# with "couldn't find DSO: libreactnative.so". Fix: extract the arm64 .so files from
# the APK into the app's lib/arm64 dir (the rooted google_apis image allows this).
# Idempotent — safe to re-run; only copies libs if they're missing.
#
#   ./scripts/install_app.sh [path/to/app.apk]      # default: apk/base.apk
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
. "$HERE/env.sh"

APK="${1:-$HERE/apk/base.apk}"
P=com.tesla.logisticsmobile
[ -f "$APK" ] || { echo "APK not found: $APK"; exit 1; }
adb devices | grep -q "emulator-.*device" || { echo "no emulator running — ./scripts/start_emulator.sh first"; exit 1; }

echo "[install] installing $APK (keeping original signature, granting runtime perms)..."
adb install -r -g "$APK"

echo "[install] rooting + locating the app lib dir..."
adb root >/dev/null 2>&1; sleep 1
CP=$(adb shell pm path $P | tr -d '\r' | grep base.apk | sed 's/package://;s#/base.apk##')
LIBDIR="$CP/lib/arm64"

if adb shell "[ -f '$LIBDIR/libreactnative.so' ]" 2>/dev/null; then
  echo "[install] arm64 libs already present in $LIBDIR — nothing to do."
else
  echo "[install] populating $LIBDIR with the APK's arm64 .so files..."
  TMP="$(mktemp -d)"
  unzip -o -j "$APK" 'lib/arm64-v8a/*.so' -d "$TMP" >/dev/null
  adb shell "mkdir -p '$LIBDIR'; rm -rf /data/local/tmp/_alibs; mkdir -p /data/local/tmp/_alibs"
  adb push "$TMP"/. /data/local/tmp/_alibs/ >/dev/null
  adb shell "cp /data/local/tmp/_alibs/*.so '$LIBDIR'/ && chown system:system '$LIBDIR'/*.so && chmod 755 '$LIBDIR'/*.so && restorecon -R '$LIBDIR' && rm -rf /data/local/tmp/_alibs"
  rm -rf "$TMP"
  echo "[install] copied $(adb shell ls '$LIBDIR' | tr -d '\r' | wc -l) libs."
fi

echo "[install] launching to verify..."
adb shell monkey -p $P -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
sleep 5
if adb shell pidof $P >/dev/null 2>&1; then
  echo "[install] OK — $P is running."
else
  echo "[install] WARNING: $P is not running; check: adb logcat -d | grep -i AndroidRuntime"
fi
