#!/usr/bin/env bash
# Run the Tesla drop-off automation as a 24/7 service.
#
# Keeps the emulator + carrier app alive and polls In-Transit on an interval; for
# every shipment that shows up it decodes each VIN's photos and completes the drop
# off. Survives emulator/app crashes (app_drive's serve loop) AND a hard Python
# crash (the restart-on-exit loop below).
#
#   ./serve.sh                 # DRY RUN, poll every 60s (safe: never taps Confirm)
#   ./serve.sh --confirm       # LIVE: actually drop off, poll every 60s
#   ./serve.sh --confirm 30    # LIVE, poll every 30s
#
# For unattended 24/7 use install the systemd unit (see tesla-delivery.service).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$HERE/.venv/bin/python"
. "$HERE/env.sh"
. "$HERE/session_env.sh"          # display env so the emulator can (re)boot headed

CONFIRM=""
INTERVAL=60
for arg in "$@"; do
  case "$arg" in
    --confirm) CONFIRM="--confirm" ;;
    [0-9]*) INTERVAL="$arg" ;;
    -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown arg: $arg"; exit 1 ;;
  esac
done

[[ -x "$PY" ]] || { echo "ERROR: $HERE/.venv missing — run ./train.sh once to bootstrap."; exit 1; }
[[ -f "$HERE/trainer/model.joblib" ]] || { echo "ERROR: no trained model — run ./train.sh train first."; exit 1; }

# Selectors run on the GPU; CLIP/OCR fully offline once the weights are cached.
export HF_HUB_DISABLE_TELEMETRY=1
ls "$HOME/.cache/huggingface/hub/"models--*CLIP* >/dev/null 2>&1 && \
  export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 || true
export CLIP_DEVICE="${CLIP_DEVICE:-cuda}"
export OCR_GPU="${OCR_GPU:-true}"

echo "[serve] starting (interval ${INTERVAL}s, ${CONFIRM:-DRY-RUN}). Ctrl-C to stop."
# Restart-on-exit: app_drive's serve() should never return, but if Python dies hard
# (segfault, OOM, etc.) bring it back after a short backoff.
while true; do
  "$PY" "$HERE/app_drive.py" --watch "$INTERVAL" $CONFIRM
  code=$?
  [[ $code -eq 130 ]] && { echo "[serve] stopped (Ctrl-C)."; exit 0; }   # SIGINT
  echo "[serve] app_drive exited ($code) — restarting in 10s…"
  sleep 10
done
