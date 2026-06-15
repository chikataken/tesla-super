#!/usr/bin/env bash
# Setup-once, run-every-time wrapper for tesla-reconcile.
#
#   ./run.sh                          -> reconciliation (test_superdispatch.py)
#   ./run.sh --count 200 --dry-run    -> reconciliation with args
#   ./run.sh login                    -> one-time Tesla/SuperDispatch login
#   ./run.sh cleanup [--apply]        -> Tesla Dispatch Dashboard cleanup
#   ./run.sh some_script.py [args]    -> run any script in this folder
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

case "$1" in
  login)    shift; exec python run_login.py "$@" ;;
  cleanup)  shift; exec python tesla_cleanup.py "$@" ;;
  *.py)     script="$1"; shift; exec python "$script" "$@" ;;
  *)        exec python test_superdispatch.py "$@" ;;
esac
