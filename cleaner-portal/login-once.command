#!/usr/bin/env bash
# cleaner-portal — ONE-TIME Tesla login. Double-click this first on a new machine (or
# whenever the session expires). It opens this folder's dedicated Chrome on the Tesla
# vendor portal; sign in, then press Return here. The session is saved into .auth and
# reused by run-cleaner.command.
set -e
cd "$(dirname "$0")"
PORTAL="$(pwd)"
. "$PORTAL/_bootstrap.sh"

echo "=========================================================="
echo "  cleaner-portal  —  one-time Tesla login"
echo "=========================================================="

check_chrome || { read -r -p "Press Return to close." _ ; exit 1; }
ensure_venv  || { read -r -p "Press Return to close." _ ; exit 1; }
portal_setup_env

"$PORTAL/.venv/bin/python" "$PORTAL/tesla_login_once.py"

echo
read -r -p "Done. Press Return to close this window." _
