#!/usr/bin/env bash
# Trainer launcher for the corner-photo classifier.
#
#   ./train.sh                 # open the labeling web app (http://localhost:8095)
#   ./train.sh pull [N]        # pull N random delivered shipments into the pool (default 5)
#   ./train.sh train [args]    # train the head on the current labels -> trainer/model.joblib
#   ./train.sh label [args]    # same as no-arg (explicit)
#
# The labeler also has a "Pull random" button (same as `pull`). Random pulls use a
# sqlite ledger (trainer/seen.db) so the SAME shipment is never repeated.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$HERE")"
PY="$HERE/.venv/bin/python"
TR_PY="$REPO_ROOT/tesla-reconcile/.venv/bin/python"
TR_DIR="$REPO_ROOT/tesla-reconcile"

# --- bootstrap this project's venv (first run) ---
if [[ ! -x "$PY" ]]; then
  command -v uv >/dev/null || { echo "ERROR: no .venv and 'uv' not found — create .venv and pip install -r requirements.txt"; exit 1; }
  echo "first run: creating .venv + installing deps…"
  uv venv "$HERE/.venv"
  uv pip install --python "$PY" -r "$HERE/requirements.txt"
fi
# CLIP weights cached -> run fully offline (trainer embeds with CLIP).
export HF_HUB_DISABLE_TELEMETRY=1
ls "$HOME/.cache/huggingface/hub/"models--*CLIP* >/dev/null 2>&1 && \
  export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 || true

cmd="${1:-label}"
case "$cmd" in
  label)
    exec "$PY" "$HERE/trainer/label_app.py" "${@:2}" ;;
  pull)
    # Orchestrated in THIS venv: scrape VINs (tesla-reconcile venv) -> fetch photos
    # via the SD API (shipment-creator venv). Both sub-venvs must exist.
    [[ -x "$TR_PY" ]] || { echo "ERROR: $TR_DIR/.venv not found — VIN scraping needs tesla-reconcile (Playwright + its SD login)."; exit 1; }
    N="${2:-20}"
    exec "$PY" "$HERE/trainer/pull_random.py" --n "$N" ;;
  train)
    exec "$PY" "$HERE/trainer/train.py" "${@:2}" ;;
  *)
    echo "usage: ./train.sh [label | pull [N] | train [args]]"; exit 1 ;;
esac
