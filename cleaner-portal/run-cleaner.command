#!/usr/bin/env bash
# cleaner-portal — double-click to run the Tesla "Dispatch Dashboard 2.0" cleanup
# (APPLY) in your REAL Chrome, attached over CDP.
#
# This folder is SELF-CONTAINED: it bundles its own copy of the cleanup code and runs
# on its own Chrome profile (.auth, created inside this folder). Drop the folder on any
# Mac with Google Chrome and it builds + runs on its own (it will install `uv` via
# Homebrew on first run if needed).
#
# What happens on a double-click:
#   1. Builds a small local .venv (first run only): Playwright + python-dotenv.
#   2. Attaches over CDP to the real Chrome on this folder's .auth profile, off-screen.
#   3. Runs the bundled tesla_cleanup.py --apply (bump Pickup dates, assign drivers).
#
# FIRST TIME on a machine: double-click login-once.command first and sign into Tesla.
# Watch the browser instead of off-screen:   ./run-cleaner.command --headed
set -e
cd "$(dirname "$0")"
PORTAL="$(pwd)"
. "$PORTAL/_bootstrap.sh"

echo "=========================================================="
echo "  cleaner-portal  —  Tesla dashboard cleanup (APPLY, Chrome/CDP)"
echo "=========================================================="

check_chrome   || { read -r -p "Press Return to close." _ ; exit 1; }
ensure_venv    || { read -r -p "Press Return to close." _ ; exit 1; }
portal_setup_env

set +e
"$PORTAL/.venv/bin/python" "$PORTAL/tesla_cleanup.py" --apply "$@"
status=$?
set -e

echo
echo "=========================================================="
if [ $status -eq 0 ]; then
  echo "  Done. (Chrome stays running with your session.)"
else
  echo "  Finished with errors (exit $status). See the messages above."
  echo "  If it said you're logged out: double-click login-once.command,"
  echo "  sign into Tesla, then run this again."
fi
echo "=========================================================="
read -r -p "Press Return to close this window." _
