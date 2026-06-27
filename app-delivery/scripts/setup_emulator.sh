#!/usr/bin/env bash
# One-time setup for the app-delivery Android emulator — NO sudo/apt required.
# Installs: a portable JDK 17, Android command-line tools, platform-tools (adb),
# the emulator, an Android 14 (google_apis) x86_64 system image, and an AVD.
#
# Idempotent / resumable: each step is skipped if already done, so re-running
# after a failed/partial download just continues.
#
#   ./scripts/setup_emulator.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
. "$HERE/env.sh"

mkdir -p "$HOME/Android"

# --- 1. Portable JDK 17 (Adoptium Temurin) ---------------------------------
if [ ! -x "$JAVA_HOME/bin/java" ]; then
  echo "[setup] downloading JDK 17 (Temurin)..."
  curl -fSL --retry 3 -o /tmp/jdk17.tgz \
    "https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jdk/hotspot/normal/eclipse"
  rm -rf "$JAVA_HOME"; mkdir -p "$JAVA_HOME"
  tar -xzf /tmp/jdk17.tgz -C "$JAVA_HOME" --strip-components=1
  rm -f /tmp/jdk17.tgz
fi
echo "[setup] java: $("$JAVA_HOME/bin/java" -version 2>&1 | head -1)"

# --- 2. Android command-line tools (sdkmanager/avdmanager) -----------------
CLT="$ANDROID_SDK_ROOT/cmdline-tools/latest"
if [ ! -x "$CLT/bin/sdkmanager" ]; then
  echo "[setup] downloading Android command-line tools..."
  curl -fSL --retry 3 -o /tmp/clt.zip \
    "https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"
  rm -rf "$ANDROID_SDK_ROOT/cmdline-tools"; mkdir -p "$ANDROID_SDK_ROOT/cmdline-tools"
  unzip -q /tmp/clt.zip -d "$ANDROID_SDK_ROOT/cmdline-tools"
  # the zip unpacks to cmdline-tools/cmdline-tools/* ; relocate to .../latest/*
  mv "$ANDROID_SDK_ROOT/cmdline-tools/cmdline-tools" "$CLT"
  rm -f /tmp/clt.zip
fi

# --- 3. Accept licenses + install SDK packages -----------------------------
echo "[setup] accepting SDK licenses..."
yes | sdkmanager --licenses >/dev/null 2>&1 || true
echo "[setup] installing platform-tools, emulator, platform + system image (~1.5GB)..."
sdkmanager --install "platform-tools" "emulator" \
  "platforms;android-${ANDROID_API}" "$ANDROID_IMG"

# --- 4. Create the AVD ------------------------------------------------------
if avdmanager list avd 2>/dev/null | grep -q "Name: $AVD_NAME"; then
  echo "[setup] AVD '$AVD_NAME' already exists."
else
  echo "[setup] creating AVD '$AVD_NAME' ($AVD_DEVICE, $ANDROID_IMG)..."
  echo "no" | avdmanager create avd -n "$AVD_NAME" -k "$ANDROID_IMG" -d "$AVD_DEVICE" --force
fi

echo
echo "[setup] DONE. Installed AVDs:"
avdmanager list avd | grep -E "Name|Based on" || true
echo "[setup] Next: grant KVM access (see README), then ./scripts/start_emulator.sh"
