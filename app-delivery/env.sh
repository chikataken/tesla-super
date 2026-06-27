# Shared environment for the app-delivery Android emulator pipeline.
# Source this (don't exec) from any script:  . "$(dirname "$0")/../env.sh"
#
# The Android SDK + a portable JDK live OUTSIDE the repo (under ~/Android) so a
# multi-GB SDK never bloats git. Only this project's code/scripts are tracked.

export ANDROID_SDK_ROOT="$HOME/Android/Sdk"
export ANDROID_HOME="$ANDROID_SDK_ROOT"          # legacy alias some tools still read
export JAVA_HOME="$HOME/Android/jdk"             # portable Temurin JDK (no apt/sudo)

export PATH="$JAVA_HOME/bin:$ANDROID_SDK_ROOT/cmdline-tools/latest/bin:$ANDROID_SDK_ROOT/platform-tools:$ANDROID_SDK_ROOT/emulator:$PATH"

# --- AVD / image config -----------------------------------------------------
# google_apis (NOT _playstore): rootable + has Google Play Services, and we
# sideload the APK with `adb install`. _playstore images can't be rooted and
# enforce stricter Play Integrity — worse for automation + photo injection.
export ANDROID_API="34"                          # Android 14
# We run the x86_64 image (KVM-accelerated, fast). The carrier APK is arm64-only,
# but the x86_64 google_apis image translates ARM via libndk_translation, so it runs.
# The ONE catch: the APK sets extractNativeLibs=false, so the installer leaves the
# app's lib/arm64 dir empty and React Native's SoLoader then looks for libs under the
# process ABI (x86_64), which the APK lacks -> "couldn't find DSO: libreactnative.so".
# scripts/install_app.sh works around it by extracting the arm64 .so files into that
# dir (needs the rooted google_apis image). See README "ARM-only app on x86_64".
export ANDROID_ABI="${ANDROID_ABI:-x86_64}"
export ANDROID_IMG="system-images;android-${ANDROID_API};google_apis;${ANDROID_ABI}"
export AVD_NAME="${AVD_NAME:-app_delivery}"
export AVD_DEVICE="pixel_6"
