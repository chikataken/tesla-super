#!/usr/bin/env bash
# cleaner-portal — install the clickable apps OUTSIDE ~/Documents so macOS lets them
# run when launched from Finder/Dock.
#
# Why: an app bundle launched from Finder/Dock can't read files in ~/Documents (macOS
# TCC privacy gate), and this repo lives there. So we copy the portal to
# ~/Applications/Cleaner Portal (not a protected location), rebuild its venv there, and
# carry over your saved Tesla login. The repo stays the source of truth — re-run this
# after you change anything to update the installed copy.
set -e
cd "$(dirname "$0")"
SRC="$(pwd)"
DEST="$HOME/Applications/Cleaner Portal"

echo "=========================================================="
echo "  Installing Cleaner Portal -> $DEST"
echo "=========================================================="
mkdir -p "$HOME/Applications"

# Copy code + bundles. Exclude the venv (rebuilt fresh), the login profile (.auth —
# seeded once below; never overwrite a live one), caches, and run logs.
rsync -a --delete \
  --exclude '.venv/' --exclude '.auth/' --exclude '__pycache__/' \
  --exclude 'output/' --exclude '.git/' --exclude '.DS_Store' \
  "$SRC/" "$DEST/"

# Carry over the saved login profile so you don't have to sign in again.
if [ -d "$SRC/.auth" ] && [ ! -d "$DEST/.auth" ]; then
  echo "Copying saved Tesla login…"
  cp -R "$SRC/.auth" "$DEST/.auth"
fi

# Build/refresh the venv in the installed location (idempotent — reuses an existing one).
echo "Setting up venv at the installed location…"
UV="$(command -v uv || command -v /opt/homebrew/bin/uv || true)"
if [ -n "$UV" ]; then
  [ -d "$DEST/.venv" ] || "$UV" venv --python 3.12 "$DEST/.venv"
  "$UV" pip install --python "$DEST/.venv/bin/python" -r "$DEST/requirements.txt"
else
  [ -d "$DEST/.venv" ] || python3 -m venv "$DEST/.venv"
  "$DEST/.venv/bin/python" -m pip install --quiet --upgrade pip
  "$DEST/.venv/bin/python" -m pip install -r "$DEST/requirements.txt"
fi

# Register the bundles so Finder/Dock see them immediately.
LS=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
[ -x "$LS" ] && "$LS" -f "$DEST/Cleaner Portal.app" "$DEST/Portal Status.app" 2>/dev/null || true

echo
echo "Done. Opening the installed folder…"
open "$DEST"
echo
echo "Next:"
echo "  * Drag 'Cleaner Portal.app' and 'Portal Status.app' to your Dock."
echo "  * If not signed in yet, double-click login-once.command there once."
echo
read -r -p "Press Return to close." _
