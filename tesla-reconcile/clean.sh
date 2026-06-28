#!/usr/bin/env bash
# Portal cleaning — Tesla "Dispatch Dashboard 2.0" end-of-day cleanup.
# Bumps every "ETA Today" / "Pickup Date Today" shipment to tomorrow.
#
# Off-screen by default (a real Chrome parked off-screen — Tesla-safe). Add --headed
# to watch it. DRY-RUN by default (counts + plan only); add --apply to submit.
#   ./clean.sh                   -> dry-run, off-screen  (safe: shows the plan, changes nothing)
#   ./clean.sh --apply           -> apply,   off-screen
#   ./clean.sh --headed          -> dry-run, visible window
#   ./clean.sh --apply --headed  -> apply,   visible window
#
# Auth is SHARED with the rest of tesla-reconcile (and shipment-creator): it drives
# the real installed Chrome over CDP on the logged-in profile (see .env / auth.py) and
# opens its work in its OWN Chrome window. If it lands on a login page, run
# `./run.sh login` once, then re-run.
set -e
cd "$(dirname "$0")"

# Make the graphical-session env available so the real Chrome can open even when
# launched from a tty / SSH / cron (see session_env.sh).
. "$(dirname "$0")/session_env.sh"

if [ ! -d .venv ]; then
  echo "First run: creating .venv and installing dependencies..."
  # Prefer uv (ships its own CPython); the stdlib venv needs ensurepip, which the
  # system Python here lacks. No `playwright install` — we drive the real Chrome
  # over CDP, so Playwright's bundled Chromium isn't needed.
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python -r requirements.txt
  else
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
  fi
fi

exec .venv/bin/python tesla_cleanup.py "$@"
