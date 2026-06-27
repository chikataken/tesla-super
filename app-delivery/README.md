# app-delivery

Android-emulator side of the Tesla delivery workflow: drive the carrier Android
app (sideloaded APK) to upload BOL photos and mark VINs delivered, fed by the
web side (Tesla portal VIN lookup/assign + SuperDispatch BOL photos).

This folder currently covers **Spike A: emulator feasibility** — can the app
install, log in, and accept uploaded photos on an emulator at all?

## Layout
```
env.sh                     # SDK/JDK paths + AVD config (sourced by scripts)
session_env.sh             # imports the desktop's DISPLAY/WAYLAND env (headed window)
scripts/setup_emulator.sh  # one-time, no-sudo: JDK + SDK + emulator + AVD
scripts/start_emulator.sh  # boot the AVD (HEADED by default; --headless for automation)
scripts/stop_emulator.sh   # clean shutdown (saves snapshot → login persists)
scripts/install_app.sh     # install the carrier APK + the arm64-lib workaround
scripts/screenshot.sh      # grab the screen → screenshots/latest.png
```

## Running the ARM-only app on the x86_64 emulator
The carrier app `com.tesla.logisticsmobile` (React Native) ships **arm64-only**
native libs and sets `extractNativeLibs=false`. The x86_64 `google_apis` image
translates ARM fine, BUT the installer leaves the app's `lib/arm64` dir empty and
SoLoader then looks for libs under the process ABI (`x86_64`) — which the APK lacks —
crashing with `couldn't find DSO: libreactnative.so`. `install_app.sh` fixes this by
extracting the APK's arm64 `.so` files into that dir (the rooted google_apis image
allows it). The original APK signature is untouched (no repackaging/re-signing).
The fix lives on the persistent userdata disk, so it survives reboots; only an APK
re-install needs it re-applied (just re-run `install_app.sh`). CodePush JS updates
don't touch native libs. NOTE: an **arm64 system image does NOT work** — the emulator
refuses arm64 on an x86_64 host ("not supported by QEMU2 emulator").
The Android SDK + a portable JDK install under `~/Android` (not in this repo).

## Prerequisites (one-time)
The emulator needs hardware acceleration. Grant your user access to `/dev/kvm`:

```bash
# immediate (no re-login), but resets on reboot:
sudo setfacl -m u:$USER:rw /dev/kvm
# OR persistent (takes effect next login):
sudo usermod -aG kvm $USER
```

No other sudo/apt is required — `setup_emulator.sh` uses a portable JDK and the
Android command-line tools.

## Usage
```bash
./scripts/setup_emulator.sh      # download + create the AVD (~2GB, one-time)
./scripts/start_emulator.sh      # boot HEADED (visible window); waits for boot_completed
./scripts/start_emulator.sh --headless   # no window (automation only)
./scripts/stop_emulator.sh       # clean shutdown

# then, with the SDK on PATH (source env.sh):
. ./env.sh
adb install -r /path/to/app.apk  # sideload the carrier app
adb shell pm list packages | grep <vendor>
```
If the headed window glitches on the NVIDIA/Wayland combo, fall back to software GL:
`EMU_GPU=swiftshader_indirect ./scripts/start_emulator.sh`.

## Spike A — what we need to learn
1. Does the app **run + log in** on an emulator, or does Play Integrity /
   attestation block it? (If blocked → physical device or a rooted+Magisk image.)
2. Is photo upload **gallery-based** (we `adb push` SD photos and pick them) or
   **camera-only** (would need feeding the emulator's virtual camera)? — the
   single most important question for the whole pipeline.
3. Are its UI elements automatable (stable resource-ids via `uiautomator dump`)?

## Notes
- Image: `system-images;android-34;google_apis;x86_64` — rootable, has Google
  Play Services, no Play Store (we sideload). Swap to `_playstore` only if the
  app strictly requires a Play-Store install.
- Headed by default so you can drive the app by hand; `session_env.sh` imports the
  desktop display env so it opens even when launched from a tty/cron. For automation
  later, `--headless` runs windowless (grab frames with `adb exec-out screencap -p`).
