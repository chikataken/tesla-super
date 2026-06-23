#!/usr/bin/env bash
# Setup-once, run-every-time wrapper for direct-pickup-checks.
#
#   ./run.sh                 -> run EVERYTHING: cloudflare tunnel + listener + worker (headed)
#   ./run.sh init            -> create .env + venv, then print credential status (no service)
#   ./run.sh tunnel          -> provision (one-time) + run only the Cloudflare tunnel
#   ./run.sh login           -> one-time Super Dispatch login (headed browser)
#   ./run.sh worker          -> just the worker (queue consumer + Playwright tagging)
#   ./run.sh listener        -> just the webhook listener (uvicorn)
#   ./run.sh subscribe [...]  -> manage webhook subscriptions (actions/list/subscribe/...)
#   ./run.sh verify [...]     -> read-only API probe (verify_api.py)
#   ./run.sh some_script.py   -> run any script in this folder
#
# The browser steps default to HEADED (visible). Override per-run, e.g.
#   HEADLESS=true ./run.sh worker     (headless — for a server)
#   WINDOW_MODE=ghost ./run.sh        (real window parked off-screen)
set -e
cd "$(dirname "$0")"

# Ensure a local .env exists (it's gitignored, so it never arrives via checkout —
# it must be created from the template). Runs on every invocation so it self-heals.
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example — EDIT IT to add your values."
  ENV_JUST_CREATED=1
fi
# The Super Dispatch credentials live in the SHARED ../secrets/.env (read by all
# three tools). Seed it from its template too if it's missing.
if [ ! -f ../secrets/.env ] && [ -f ../secrets/.env.example ]; then
  cp ../secrets/.env.example ../secrets/.env
  echo "Created ../secrets/.env from its template — add SUPERDISPATCH_CLIENT_ID/_SECRET there."
  ENV_JUST_CREATED=1
fi
if [ -n "${ENV_JUST_CREATED:-}" ]; then
  echo "   Credentials needed before live runs: SUPERDISPATCH_CLIENT_ID, "
  echo "   SUPERDISPATCH_CLIENT_SECRET (and SD_WEBHOOK_VERIFICATION_TOKEN for the listener)."
fi

if [ ! -d .venv ]; then
  echo "First run: creating .venv and installing dependencies..."
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  python -m playwright install chromium
else
  source .venv/bin/activate
fi

# Default to a visible browser unless the caller overrides.
export HEADLESS="${HEADLESS:-false}"
export WINDOW_MODE="${WINDOW_MODE:-visible}"

# Resolve settings from config (.env is the source of truth) into shell vars.
read -r LISTENER_HOST LISTENER_PORT TUNNEL_NAME TUNNEL_HOSTNAME < <(python -c \
  "import config;print(config.LISTENER_HOST, config.LISTENER_PORT, config.TUNNEL_NAME, config.TUNNEL_HOSTNAME or '-')")

CF_DIR=".cloudflared"
CF_CONFIG="$CF_DIR/config.yml"

# --------------------------------------------------------------------------
# Cloudflare named tunnel: install -> login (one-time) -> create -> route DNS
# -> write config.yml. Idempotent: each step is skipped if already done.
# --------------------------------------------------------------------------
ensure_cloudflared_installed() {
  command -v cloudflared >/dev/null 2>&1 && return 0
  echo "cloudflared not found — installing..."
  if command -v brew >/dev/null 2>&1; then
    brew install cloudflared
  elif command -v apt-get >/dev/null 2>&1; then
    arch="$(dpkg --print-architecture)"
    url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${arch}.deb"
    tmp="$(mktemp --suffix=.deb)"; curl -fsSL "$url" -o "$tmp"
    sudo dpkg -i "$tmp"; rm -f "$tmp"
  else
    echo "ERROR: install cloudflared manually:"
    echo "  https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
    exit 1
  fi
}

provision_tunnel() {
  if [ -z "$TUNNEL_HOSTNAME" ] || [ "$TUNNEL_HOSTNAME" = "-" ]; then
    echo "ERROR: TUNNEL_HOSTNAME not set in .env (e.g. test.wastake.com)."; exit 1
  fi
  ensure_cloudflared_installed

  # One-time browser login that authorizes your Cloudflare account + the zone.
  if [ ! -f "$HOME/.cloudflared/cert.pem" ]; then
    echo ">> Cloudflare login (one-time): a browser opens — pick the wastake.com zone."
    cloudflared tunnel login
  fi

  # Create the named tunnel if it doesn't exist yet.
  if ! cloudflared tunnel list 2>/dev/null | awk '{print $2}' | grep -qx "$TUNNEL_NAME"; then
    echo ">> Creating tunnel '$TUNNEL_NAME'..."
    cloudflared tunnel create "$TUNNEL_NAME"
  fi

  # Resolve the tunnel UUID + its credentials file (~/.cloudflared/<UUID>.json).
  TUNNEL_ID="$(cloudflared tunnel list 2>/dev/null | awk -v n="$TUNNEL_NAME" '$2==n{print $1}')"
  if [ -z "$TUNNEL_ID" ]; then echo "ERROR: could not resolve tunnel id for $TUNNEL_NAME"; exit 1; fi
  CRED="$HOME/.cloudflared/$TUNNEL_ID.json"

  # Write the ingress config: hostname -> the loopback listener.
  mkdir -p "$CF_DIR"
  cat > "$CF_CONFIG" <<EOF
tunnel: $TUNNEL_ID
credentials-file: $CRED
ingress:
  - hostname: $TUNNEL_HOSTNAME
    service: http://127.0.0.1:$LISTENER_PORT
  - service: http_status:404
EOF

  # Route the public hostname to this tunnel (creates the DNS CNAME on the zone).
  # Idempotent: if the record already points here, cloudflared exits non-zero — ignore.
  echo ">> Routing $TUNNEL_HOSTNAME -> tunnel $TUNNEL_NAME ..."
  cloudflared tunnel route dns "$TUNNEL_NAME" "$TUNNEL_HOSTNAME" 2>/dev/null \
    || echo "   (DNS route already exists — ok)"
  echo ">> Tunnel ready: https://$TUNNEL_HOSTNAME -> 127.0.0.1:$LISTENER_PORT"
}

run_tunnel() {            # provision (idempotent) then run in the foreground
  provision_tunnel
  exec cloudflared tunnel --config "$CF_CONFIG" run "$TUNNEL_NAME"
}

case "$1" in
  init)      echo "--- credential status ---"; exec python sd_client.py ;;
  tunnel)    run_tunnel ;;
  login)     shift; exec python run_login.py "$@" ;;
  worker)    shift; exec python worker.py "$@" ;;
  listener)  shift; exec python -m uvicorn listener:app \
               --host "$LISTENER_HOST" --port "$LISTENER_PORT" "$@" ;;
  subscribe) shift; exec python subscribe.py "$@" ;;
  verify)    shift; exec python verify_api.py "$@" ;;
  *.py)      script="$1"; shift; exec python "$script" "$@" ;;
  "")        # default: EVERYTHING — tunnel + worker in the background, listener in front.
             provision_tunnel
             echo "Starting tunnel + worker + listener (headed). Ctrl+C stops all."
             cloudflared tunnel --config "$CF_CONFIG" run "$TUNNEL_NAME" &
             TUNNEL_PID=$!
             python worker.py &
             WORKER_PID=$!
             trap 'kill "$TUNNEL_PID" "$WORKER_PID" 2>/dev/null || true' EXIT INT TERM
             python -m uvicorn listener:app --host "$LISTENER_HOST" --port "$LISTENER_PORT"
             ;;
  *)         echo "Unknown command: $1"
             sed -n '2,18p' "$0"
             exit 2 ;;
esac
