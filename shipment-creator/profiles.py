"""Dispatcher profiles — who is running the board.

A profile does two things:
  1. Filters the incoming Excel to the states that dispatcher covers, so only their
     VINs are scraped (cutting down the run).
  2. Supplies the phone number that fills the <dispatcher> token in the SuperDispatch
     load-board + order instructions (see config.render_dispatcher / sd_api.to_sd_order).

Profiles live as plain JSON in <data dir>/profiles/profiles.json so they're easy to
find and edit (phones + states get filled in there). The selected profile id is
persisted in <data dir>/active_profile.json and survives restarts — it is NOT cleared
by a board reset.
"""
import json
import os

import paths

PROFILES_DIR = os.path.join(paths.DATA_DIR, "profiles")
PROFILES_PATH = os.path.join(PROFILES_DIR, "profiles.json")
# Active selection lives at the data-dir root (NOT under output/), so a board reset
# leaves the chosen dispatcher in place.
ACTIVE_PATH = os.path.join(paths.DATA_DIR, "active_profile.json")

# Seeded on first run if profiles.json doesn't exist yet. Fill in `phone` (the number
# the <dispatcher> token becomes) and `states` (2-letter codes this dispatcher pulls
# from the Excel; an empty list means "no state filter yet" → all VINs pass).
DEFAULT_PROFILES = [
    {"id": "soyo",  "name": "Soyo",  "phone": "", "states": []},
    {"id": "kelly", "name": "Kelly", "phone": "", "states": []},
    {"id": "duka",  "name": "Duka",  "phone": "", "states": []},
    {"id": "burte", "name": "Burte", "phone": "", "states": []},
]


def _seed_if_missing() -> None:
    if os.path.exists(PROFILES_PATH):
        return
    os.makedirs(PROFILES_DIR, exist_ok=True)
    with open(PROFILES_PATH, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_PROFILES, f, indent=2)


def list_profiles() -> list:
    """All dispatcher profiles (seeding the defaults on first use)."""
    _seed_if_missing()
    try:
        with open(PROFILES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else list(DEFAULT_PROFILES)
    except (OSError, ValueError):
        return list(DEFAULT_PROFILES)


def get_profile(pid: str | None) -> dict | None:
    if not pid:
        return None
    return next((p for p in list_profiles() if p.get("id") == pid), None)


def active_id() -> str | None:
    try:
        with open(ACTIVE_PATH, encoding="utf-8") as f:
            return (json.load(f) or {}).get("id") or None
    except (OSError, ValueError):
        return None


def set_active(pid: str | None) -> None:
    """Persist the selected profile id. Pass None/'' to clear it."""
    pid = pid or None
    if pid is not None and not get_profile(pid):
        raise ValueError(f"unknown dispatcher profile: {pid}")
    os.makedirs(paths.DATA_DIR, exist_ok=True)
    with open(ACTIVE_PATH, "w", encoding="utf-8") as f:
        json.dump({"id": pid}, f)


def active_profile() -> dict | None:
    return get_profile(active_id())


def dispatcher_phone(profile: dict | None = None) -> str | None:
    """The <dispatcher> fill value for a profile (defaults to the active one)."""
    p = profile if profile is not None else active_profile()
    return ((p or {}).get("phone") or "").strip() or None


# --------------------------- Excel state filtering ---------------------------
def _norm_state(s) -> str:
    """Normalize a state (full name or code) to its 2-letter code, uppercased."""
    import sd_api
    return sd_api._state(str(s or "").strip()).upper()


def allowed_states(profile: dict | None) -> set:
    return {_norm_state(s) for s in (profile or {}).get("states", []) if str(s).strip()}


def filter_rows(rows: list, profile: dict | None):
    """Keep only the rows whose PICKUP state the profile covers. An empty `states`
    list (not configured yet) means keep everything, so the app works until the
    profiles are filled in."""
    want = allowed_states(profile)
    if not want:
        return list(rows)
    return [r for r in rows if _norm_state(r.get("pickup_state")) in want]
