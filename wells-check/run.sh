#!/usr/bin/env bash
# Setup-once, run-every-time wrapper for wells-check (Wells Fargo check reconciliation).
#
#   ./run.sh                      -> scrape the SD Paid tab AND enrich via the API as it
#                                    goes (RESUMES where it left off; Ctrl+C any time,
#                                    position is saved per page, pending API rows drain
#                                    on the next run)
#   ./run.sh --pages 50           -> at most 50 pages this run
#   ./run.sh --restart            -> restart the scan from page 1 (db rows are kept)
#   ./run.sh --topup              -> catch NEWLY-paid orders (scan-only from page 1, stops
#                                    when nothing new; backfill cursor stays put)
#   ./run.sh enrich [--limit N]   -> API enrichment only (e.g. after a --topup)
#   ./run.sh stats                -> DB counts + scan position
#   ./run.sh some_script.py [...] -> run any script in this folder
#
# Uses the SHARED logged-in Chrome on :9222 (tesla-reconcile/.auth profile) via
# tesla-reconcile's auth module — the chrome-cdp user service normally keeps that
# Chrome alive; if it isn't running, one is launched (needs the graphical session).
set -e
cd "$(dirname "$0")"

# Graphical-session env so a Chrome launch works from a tty/SSH (same helper the
# other tools use). Only matters when the shared Chrome isn't already up.
. ../tesla-reconcile/session_env.sh

if [ ! -d .venv ]; then
  echo "First run: creating .venv and installing dependencies..."
  if command -v uv >/dev/null 2>&1; then                 # repo standard (python3.14 lacks venv)
    uv venv .venv >/dev/null
    uv pip install -q -r requirements.txt --python .venv/bin/python
  else
    python3 -m venv .venv
    .venv/bin/pip install -q -r requirements.txt
  fi
fi
source .venv/bin/activate

case "$1" in
  enrich)   shift; exec python enrich.py "$@" ;;
  stats)    exec python db.py ;;
  *.py)     script="$1"; shift; exec python "$script" "$@" ;;
  *)        exec python scrape.py "$@" ;;
esac
