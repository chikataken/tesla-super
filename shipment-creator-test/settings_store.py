"""User-facing settings, so a non-technical user can configure the app from the GUI
instead of editing a .env file.

- Non-secret values (SuperDispatch environment + client id, the Excel path, Chrome
  path, the login profile dir) are stored as plain JSON in `settings.json` under the
  writable data dir.
- The SuperDispatch client SECRET is stored in the Windows Credential Manager via
  `keyring` (never written to the JSON). If keyring isn't available, it falls back to
  the JSON file so the app still works.

`apply_to_env()` layers these underneath real environment variables (setdefault), so
an actual env var or a .env entry always wins. config.py calls it at import.
"""
import json
import os

import paths

# Non-secret keys the GUI can edit; each maps 1:1 to an env var config.py reads.
PUBLIC_KEYS = [
    "SD_ENV",                    # "test" | "production"
    "SUPERDISPATCH_CLIENT_ID",
    "EXCEL_PATH",                # default sheet the GUI/pipeline uses
    "CHROME_PATH",               # optional explicit chrome.exe
    "CDP_PROFILE_DIR",           # persistent Chrome profile for the one-time login
]
SECRET_KEY = "SUPERDISPATCH_CLIENT_SECRET"
_KEYRING_SERVICE = "TFI Shipment Creator"


def load() -> dict:
    try:
        with open(paths.SETTINGS_PATH, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(d: dict) -> None:
    os.makedirs(paths.DATA_DIR, exist_ok=True)
    with open(paths.SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def get_secret() -> str:
    """The SD client secret, from the OS keyring, or the JSON fallback."""
    try:
        import keyring
        v = keyring.get_password(_KEYRING_SERVICE, SECRET_KEY)
        if v:
            return v
    except Exception:                               # noqa: BLE001 - keyring optional
        pass
    return str(load().get(SECRET_KEY, "") or "")


def _set_secret(secret: str) -> None:
    try:
        import keyring
        keyring.set_password(_KEYRING_SERVICE, SECRET_KEY, secret)
        # make sure no stale plaintext copy lingers in the JSON
        d = load()
        if d.pop(SECRET_KEY, None) is not None:
            _write(d)
        return
    except Exception:                               # noqa: BLE001 - keyring unavailable
        d = load()
        d[SECRET_KEY] = secret                      # last-resort plaintext fallback
        _write(d)


def save(values: dict) -> None:
    """Persist the given subset of settings. Only known keys are written; a missing
    key leaves the stored value untouched, and an empty string clears it."""
    d = load()
    for k in PUBLIC_KEYS:
        if k in values and values[k] is not None:
            d[k] = str(values[k]).strip()
    _write(d)
    if SECRET_KEY in values and values[SECRET_KEY]:
        _set_secret(str(values[SECRET_KEY]).strip())


def apply_to_env(force: bool = False) -> None:
    """Push saved settings into os.environ. By default uses setdefault (real env vars
    win); `force=True` overrides — used right after a save so a config reload sees the
    new values."""
    d = load()
    for k in PUBLIC_KEYS:
        v = d.get(k)
        if v in (None, ""):
            continue
        if force:
            os.environ[k] = str(v)
        else:
            os.environ.setdefault(k, str(v))
    sec = get_secret()
    if sec:
        if force:
            os.environ[SECRET_KEY] = sec
        else:
            os.environ.setdefault(SECRET_KEY, sec)


def public_view() -> dict:
    """What the Settings GUI reads back: non-secret values plus whether a secret is
    set (the secret itself is never returned)."""
    d = load()
    out = {k: d.get(k, "") for k in PUBLIC_KEYS}
    out["has_secret"] = bool(get_secret())
    return out
