#!/usr/bin/env bash
# Portal cleaning — Tesla "Dispatch Dashboard 2.0" end-of-day cleanup.
# Bumps every "ETA Today" / "Pickup Date Today" shipment to tomorrow.
#
# HEADLESS by default (a real Chrome parked off-screen — Tesla-safe). Add --headed
# to watch it. DRY-RUN by default (counts + plan only); add --apply to submit.
#   ./clean.sh                   -> dry-run, headless   (safe: shows the plan, changes nothing)
#   ./clean.sh --apply           -> apply,   headless
#   ./clean.sh --headed          -> dry-run, visible window
#   ./clean.sh --apply --headed  -> apply,   visible window
#
# Login is shared with the rest of tesla-reconcile — if it lands on a login page,
# run `./run.sh login` once, then re-run.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "First run: creating .venv and installing dependencies..."
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  python -m playwright install chromium
else
  source .venv/bin/activate
fi

exec python tesla_cleanup.py "$@"
