#!/usr/bin/env bash
# Boot the app-delivery AVD and wait for a full Android boot.
#
# HEADED by default (a real window on your Wayland desktop) so you can drive the
# app by hand. session_env.sh imports the graphical-session environment, so this
# works even when launched from a bare tty / this Claude session / cron.
#
#   ./scripts/start_emulator.sh                 # headed (visible window) — default
#   ./scripts/start_emulator.sh --headless      # no window (for automation only)
#   EMU_GPU=swiftshader_indirect ./scripts/start_emulator.sh   # if the window glitches
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
. "$HERE/env.sh"
. "$HERE/session_env.sh"          # import DISPLAY/WAYLAND/XAUTHORITY for a headed window

MODE="headed"
[ "${1:-}" = "--headless" ] && MODE="headless"

if ! test -w /dev/kvm; then
  echo "ERROR: /dev/kvm is not writable by $(id -un). Grant it first (one-time):"
  echo "    sudo setfacl -m u:$(id -un):rw /dev/kvm     # immediate, no re-login"
  exit 1
fi

# Already running?
if adb devices 2>/dev/null | grep -q "emulator-.*device"; then
  echo "emulator already booted:"; adb devices
  echo "(stop it first with ./scripts/stop_emulator.sh to relaunch in $MODE mode)"
  exit 0
fi

# GPU renderer choice on this box (RTX 5060 / driver 595, Wayland):
#   * host                -> glAttachShader 0x502 errors -> SurfaceFlinger stalls + ANRs. NO.
#   * swiftshader_indirect-> stable but a broken framebuffer: typed text never paints,
#                            duplicated/stale frames. NO.
#   * swangle_indirect    -> ANGLE over (sw) Vulkan. Renders correctly (text paints,
#                            no stale frames), no ANRs. THIS. Override with EMU_GPU=...
GPU="${EMU_GPU:-swangle_indirect}"
if [ "$MODE" = "headed" ]; then
  echo "[start] booting $AVD_NAME HEADED (window on $XDG_SESSION_TYPE display ${WAYLAND_DISPLAY:-$DISPLAY}, gpu $GPU)..."
  WINFLAGS=(-gpu "$GPU")
else
  echo "[start] booting $AVD_NAME HEADLESS (no window, gpu $GPU)..."
  WINFLAGS=(-no-window -gpu "$GPU")
fi

# No -no-snapshot: let the AVD save/restore state so your app login survives reboots.
# NOTE on hw.ramSize in config.ini — two traps (the guest ran at 2560M for weeks
# after config.ini said "4096M", and the low-RAM guest's am_low_memory kills are
# what zombie the app):
#   1. The value must be a PLAIN MB integer ("4096"). "4096M" doesn't parse — the
#      emulator silently falls back to its own sizing ("Increasing RAM size to
#      2560MB" in emulator.log).
#   2. The cached hardware-qemu.ini + snapshots pin the old value. To apply a RAM
#      change: stop the emulator, then
#        rm -rf ~/.android/avd/$AVD_NAME.avd/snapshots ~/.android/avd/$AVD_NAME.avd/hardware-qemu.ini
#      and let the next boot (cold, one-time) rebuild both. Login survives — it
#      lives on the userdata disk, not in the snapshot.
nohup emulator -avd "$AVD_NAME" -no-audio -no-boot-anim "${WINFLAGS[@]}" \
  > "$HERE/emulator.log" 2>&1 &
EMU_PID=$!
echo "[start] emulator PID $EMU_PID (log: $HERE/emulator.log)"

# Bail-fast helper: if the emulator process has died (e.g. FATAL 'arch not supported',
# GL init failure), stop waiting and surface the log instead of hanging in adb forever.
_emu_dead() {
  if ! kill -0 "$EMU_PID" 2>/dev/null; then
    echo "[start] ERROR: emulator exited during startup. Last log lines:"
    tail -8 "$HERE/emulator.log"
    return 0
  fi
  return 1
}

echo "[start] waiting for device to register (max ~5 min)..."
for _ in $(seq 1 150); do
  _emu_dead && exit 1
  adb devices 2>/dev/null | grep -qE "emulator-[0-9]+" && break
  sleep 2
done
echo "[start] waiting for full boot (sys.boot_completed, max ~10 min)..."
for _ in $(seq 1 300); do
  _emu_dead && exit 1
  [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && break
  sleep 2
done
adb shell input keyevent 82 >/dev/null 2>&1 || true   # dismiss the lockscreen

# Performance tuning for the software-rendering path on this box: disable animations
# and render at a lower resolution (SAME dp layout: 720x1600@280 == 1080x2400@420 in dp)
# so swANGLE keeps up. Without this the app skips frames -> "not responding" ANRs.
# Applied now, while only the launcher is up — changing resolution under a RUNNING
# React Native app crashes it into its error boundary. Override via EMU_RES / EMU_DPI.
adb shell settings put global window_animation_scale 0 >/dev/null 2>&1 || true
adb shell settings put global transition_animation_scale 0 >/dev/null 2>&1 || true
adb shell settings put global animator_duration_scale 0 >/dev/null 2>&1 || true
adb shell wm size "${EMU_RES:-720x1600}" >/dev/null 2>&1 || true
adb shell wm density "${EMU_DPI:-280}" >/dev/null 2>&1 || true
echo "[start] boot complete (animations off, ${EMU_RES:-720x1600}@${EMU_DPI:-280}):"
adb devices
