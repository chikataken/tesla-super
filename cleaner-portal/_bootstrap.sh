# Shared bootstrap for cleaner-portal's .command launchers. Sourced, not executed.
# Defines: portal_setup_env  and  ensure_venv
# Requires PORTAL to be set by the caller (absolute path to this folder).

# Make Homebrew tools (uv, etc.) reachable even from a double-clicked .command,
# which starts with a minimal PATH.
case ":$PATH:" in *":/opt/homebrew/bin:"*) ;; *) PATH="/opt/homebrew/bin:$PATH" ;; esac
case ":$PATH:" in *":/usr/local/bin:"*) ;; *) PATH="/usr/local/bin:$PATH" ;; esac
export PATH

portal_setup_env() {
  # Attach over CDP to the real Chrome, on a profile that lives INSIDE this folder,
  # so the whole thing is self-contained and portable.
  export AUTH_MODE=cdp
  export CDP_PROFILE_DIR="$PORTAL/.auth"
  export PYTHONUNBUFFERED=1            # so run logs flush live (no block buffering)
}

_have() { command -v "$1" >/dev/null 2>&1; }

ensure_venv() {
  [ -d "$PORTAL/.venv" ] && return 0
  echo "First run: creating .venv (Playwright + python-dotenv)..."

  if _have uv; then
    uv venv --python 3.12 "$PORTAL/.venv" \
      && uv pip install --python "$PORTAL/.venv/bin/python" -r "$PORTAL/requirements.txt"
    return $?
  fi

  # No uv. The cleanup code needs Python >=3.10 (uses `str | None` syntax), and the
  # stock macOS python3 is 3.9 — so install uv via Homebrew if we can.
  if _have brew; then
    echo "Installing uv via Homebrew (one-time)..."
    if brew install uv; then
      uv venv --python 3.12 "$PORTAL/.venv" \
        && uv pip install --python "$PORTAL/.venv/bin/python" -r "$PORTAL/requirements.txt"
      return $?
    fi
  fi

  # Last resort: a system Python >=3.10 already on PATH.
  local c ver pybin=""
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if _have "$c"; then
      ver=$("$c" -c 'import sys;print("%d%02d"%sys.version_info[:2])' 2>/dev/null)
      if [ -n "$ver" ] && [ "$ver" -ge 310 ]; then pybin="$c"; break; fi
    fi
  done
  if [ -z "$pybin" ]; then
    echo
    echo "Could not set up Python. This needs ONE of:"
    echo "  * uv          ->  install Homebrew (brew.sh), then: brew install uv"
    echo "  * Python 3.10+ on PATH"
    return 1
  fi
  "$pybin" -m venv "$PORTAL/.venv" \
    && "$PORTAL/.venv/bin/python" -m pip install --quiet --upgrade pip \
    && "$PORTAL/.venv/bin/python" -m pip install -r "$PORTAL/requirements.txt"
}

check_chrome() {
  if [ ! -d "/Applications/Google Chrome.app" ] && ! _have "google-chrome"; then
    echo "Google Chrome doesn't appear to be installed (/Applications/Google Chrome.app)."
    echo "Install Chrome, then run this again."
    return 1
  fi
  return 0
}
