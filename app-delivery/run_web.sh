#!/usr/bin/env bash
# Launcher for the app-delivery status dashboard (app.wastake.com), for systemd.
# Serves dashboard.py on 127.0.0.1:$PORT (default 8011); the shared cloudflared tunnel
# routes app.wastake.com here. Dependency-free (stdlib http.server) — uses the
# app-delivery venv python if present, else system python3.
set -e
cd "$(dirname "$0")"
export PORT="${PORT:-8011}"
PY=".venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
exec "$PY" dashboard.py --port "$PORT" --host 127.0.0.1
