#!/usr/bin/env bash
# One command to set up (first run) and run (every run).
#   ./run.sh --excel sheet.xlsx
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

python main.py "$@"
