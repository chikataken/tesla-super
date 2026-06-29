#!/usr/bin/env bash
# Fetch a VIN's SuperDispatch Delivery photos and output the 4 corner shots
# (front, rear, and both sides) to a folder — all locally (no Claude/API).
# Selector: CLIP zero-shot (photo_select_clip.py). Override with SELECTOR=yolo|clip.
#
#   ./run.sh --vin 7SAXCDE55PF381263 [--out DIR]
#
# Output (DIR defaults to ./out):
#   <DIR>/<VIN>/front.jpg  rear.jpg  left_side.jpg  right_side.jpg
#   <DIR>/<VIN>/picks.png        annotated contact sheet (the 4 picks boxed)
#   <DIR>/<VIN>/picks.json       full classification + chosen indices
#   <DIR>/<VIN>/_source/         the raw Delivery photos that were fetched
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$HERE")"
SC="$REPO_ROOT/shipment-creator"                  # sibling: SD photo fetcher
WEIGHTS="$HERE/models/carparts_yolo11n_seg.pt"
WEIGHTS_URL="https://huggingface.co/konst22/yolo11n-carparts-seg/resolve/main/best.pt"
PY="$HERE/.venv/bin/python"
SC_PY="$SC/.venv/bin/python"
# Selector: default to the TRAINED head once it exists, else CLIP zero-shot.
# Override explicitly with SELECTOR=trained|clip|yolo.
if [[ -z "${SELECTOR:-}" ]]; then
  if [[ -f "$HERE/trainer/model.joblib" ]]; then SELECTOR=trained; else SELECTOR=clip; fi
fi
case "$SELECTOR" in
  trained) SELECT_SCRIPT="photo_select_trained.py";;
  clip)    SELECT_SCRIPT="photo_select_clip.py";;
  yolo)    SELECT_SCRIPT="photo_select_yolo.py";;
  *) echo "unknown SELECTOR='$SELECTOR' (use trained|clip|yolo)"; exit 1;;
esac
if [[ "$SELECTOR" == "trained" && ! -f "$HERE/trainer/model.joblib" ]]; then
  echo "ERROR: SELECTOR=trained but no trainer/model.joblib — label (trainer/label_app.py) then train (trainer/train.py)."; exit 1
fi

usage() { echo "usage: ./run.sh --vin <VIN> [--out DIR]"; }

VIN=""; OUT="$HERE/out"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --vin)  VIN="${2:-}"; shift 2;;
    --vin=*) VIN="${1#*=}"; shift;;
    --out)  OUT="${2:-}"; shift 2;;
    --out=*) OUT="${1#*=}"; shift;;
    -h|--help) usage; exit 0;;
    *) echo "unknown arg: $1"; usage; exit 1;;
  esac
done
[[ -z "$VIN" ]] && { usage; exit 1; }

# --- bootstrap this project's venv (first run) ---
if [[ ! -x "$PY" ]]; then
  command -v uv >/dev/null || { echo "ERROR: no .venv and 'uv' not found — create .venv and pip install -r requirements.txt"; exit 1; }
  echo "first run: creating .venv + installing deps…"
  uv venv "$HERE/.venv"
  uv pip install --python "$PY" -r "$HERE/requirements.txt"
fi
# --- fetch step needs the sibling's venv (it loads shipment-creator's own config) ---
[[ -x "$SC_PY" ]] || { echo "ERROR: $SC/.venv not found — set up shipment-creator (it fetches the SD photos)"; exit 1; }
# --- car-parts weights (yolo selector only; CLIP downloads its own on first use) ---
if [[ "$SELECTOR" == "yolo" && ! -f "$WEIGHTS" ]]; then
  echo "downloading car-parts weights…"
  mkdir -p "$HERE/models"
  curl -fsSL -o "$WEIGHTS" "$WEIGHTS_URL"
fi

# Once the CLIP weights are cached, run FULLY OFFLINE: skip the HF Hub revision check
# (and its "unauthenticated request" warning). The first run still downloads the model.
export HF_HUB_DISABLE_TELEMETRY=1
if [[ "$SELECTOR" == "clip" || "$SELECTOR" == "trained" ]] && \
   ls "$HOME/.cache/huggingface/hub/"models--*CLIP* >/dev/null 2>&1; then
  export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
fi

DEST="$OUT/$VIN"
SRC="$DEST/_source"
mkdir -p "$SRC"

echo "[1/2] fetching Delivery photos for $VIN…"
( cd "$SC" && "$SC_PY" sd_photos.py "$VIN" --type Delivery --out "$SRC" )

shopt -s nullglob
photos=("$SRC"/*.jpg "$SRC"/*.jpeg "$SRC"/*.png)
if [[ ${#photos[@]} -eq 0 ]]; then
  echo "no Delivery photos found for $VIN — nothing to select."; exit 2
fi

echo "[2/2] selecting front / rear / sides from ${#photos[@]} photo(s) (selector=$SELECTOR)…"
"$PY" "$HERE/$SELECT_SCRIPT" "$SRC" \
  --copy-dir "$DEST" --out "$DEST/picks.json" --sheet "$DEST/picks.png"

echo
echo "done -> $DEST"
ls -1 "$DEST"/*.jpg 2>/dev/null || true
