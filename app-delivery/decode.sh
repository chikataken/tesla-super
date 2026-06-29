#!/usr/bin/env bash
# Decode a VIN into the 3 Drop-Off photo sets (all local; no Claude):
#   sides/ (6 corners via the trained model) · vin_plate/ (on-device OCR) · key/ (same)
#
#   ./decode.sh --vin 7SAXCDE55PF381263 [--out DIR]
#
# Fetches the LATEST DELIVERED shipment's photos via the SD API, runs the trained
# corner model + the OCR VIN reader. Needs the trained model (trainer/model.joblib)
# and the shipment-creator + direct-pickup-checks venvs (for the API + OCR).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$HERE/.venv/bin/python"

usage() { echo "usage: ./decode.sh --vin <VIN> [--out DIR]"; }
VIN=""; OUT="$HERE/out"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --vin) VIN="${2:-}"; shift 2;;
    --vin=*) VIN="${1#*=}"; shift;;
    --out) OUT="${2:-}"; shift 2;;
    --out=*) OUT="${1#*=}"; shift;;
    -h|--help) usage; exit 0;;
    *) echo "unknown arg: $1"; usage; exit 1;;
  esac
done
[[ -z "$VIN" ]] && { usage; exit 1; }

[[ -x "$PY" ]] || { echo "ERROR: $HERE/.venv missing — run ./train.sh once to bootstrap."; exit 1; }
[[ -f "$HERE/trainer/model.joblib" ]] || { echo "ERROR: no trained model — run ./train.sh train first."; exit 1; }

# app-delivery is GPU-based (direct-pickup runs on CPU). CLIP fully offline once cached.
export HF_HUB_DISABLE_TELEMETRY=1
ls "$HOME/.cache/huggingface/hub/"models--*CLIP* >/dev/null 2>&1 && \
  export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 || true
export CLIP_DEVICE="${CLIP_DEVICE:-cuda}"     # corner model on GPU
export OCR_GPU="${OCR_GPU:-true}"             # VIN-plate easyOCR on GPU too

exec "$PY" "$HERE/decode_vin.py" --vin "$VIN" --out "$OUT"
