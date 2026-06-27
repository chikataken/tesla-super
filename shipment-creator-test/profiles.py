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
import re
import shutil

import paths

PROFILES_DIR = os.path.join(paths.DATA_DIR, "profiles")
PROFILES_PATH = os.path.join(PROFILES_DIR, "profiles.json")
# Active selection lives at the data-dir root (NOT under output/), so a board reset
# leaves the chosen dispatcher in place.
ACTIVE_PATH = os.path.join(paths.DATA_DIR, "active_profile.json")
# Bundled (read-only) copy shipped with the app. In dev this is the same source
# folder; in the frozen build it's PyInstaller's unpack dir, used to seed the
# writable copy and to fall back for avatar images.
_BUNDLED_DIR = paths.resource_path("profiles")

# Seeded on first run if profiles.json doesn't exist yet. Fill in `phone` (the number
# the <dispatcher> token becomes) and `states` (2-letter codes this dispatcher pulls
# from the Excel; an empty list means "no state filter yet" → all VINs pass).
# The "all" profile is special: it NEVER filters by state (see allowed_states), so it
# pulls every VIN in the Excel — handy for feeding the terminal cache without a region cap.
ALL_PROFILE_ID = "all"

DEFAULT_PROFILES = [
    {"id": "all",   "name": "ALL",   "phone": "", "states": []},
    {"id": "soyo",  "name": "Soyo",  "phone": "", "states": []},
    {"id": "kelly", "name": "Kelly", "phone": "", "states": []},
    {"id": "duka",  "name": "Duka",  "phone": "", "states": []},
    {"id": "burte", "name": "Burte", "phone": "", "states": []},
]


def _seed_if_missing() -> None:
    if os.path.exists(PROFILES_PATH):
        return
    os.makedirs(PROFILES_DIR, exist_ok=True)
    # Prefer the bundled profiles.json (carries the configured phones/states) over the
    # bare defaults — but never copy a file onto itself (dev: bundle == writable).
    bundled = os.path.join(_BUNDLED_DIR, "profiles.json")
    if os.path.exists(bundled) and os.path.abspath(bundled) != os.path.abspath(PROFILES_PATH):
        shutil.copyfile(bundled, PROFILES_PATH)
        return
    with open(PROFILES_PATH, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_PROFILES, f, indent=2)


def image_dirs() -> list:
    """Folders to look in for a dispatcher avatar: the writable profiles dir first,
    then the bundled one (frozen build), de-duped."""
    dirs = [os.path.join(PROFILES_DIR, "images")]
    bundled = os.path.join(_BUNDLED_DIR, "images")
    if os.path.abspath(bundled) != os.path.abspath(dirs[0]):
        dirs.append(bundled)
    return dirs


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


def parse_states(states) -> list:
    """Normalize a states value (a string like 'VA MD GA FL' or a list) to a clean
    list of uppercase tokens. Comma- and whitespace-separated both work."""
    toks = re.split(r"[,\s]+", states) if isinstance(states, str) else (states or [])
    return [str(s).strip().upper() for s in toks if str(s).strip()]


def save_profile(pid: str, phone=None, states=None, name=None) -> dict:
    """Update a profile's name, phone and/or states in profiles.json (id preserved).
    `states` may be a string ('VA MD GA') or a list."""
    profs = list_profiles()
    p = next((x for x in profs if x.get("id") == pid), None)
    if not p:
        raise ValueError(f"unknown dispatcher profile: {pid}")
    if name is not None and str(name).strip():
        p["name"] = str(name).strip()
    if phone is not None:
        p["phone"] = str(phone).strip()
    if states is not None:
        new_states = parse_states(states)
        p["states"] = new_states
        # Invariant: a state belongs to AT MOST ONE dispatcher. Saving these states to this
        # user strips them from every other user (the 'all' profile has none). This keeps
        # profiles.json consistent no matter what order the cards are saved in.
        if pid != ALL_PROFILE_ID:
            claimed = set(new_states)
            for other in profs:
                if other is p or other.get("id") == ALL_PROFILE_ID:
                    continue
                if other.get("states"):
                    other["states"] = [s for s in other["states"] if s not in claimed]
    os.makedirs(PROFILES_DIR, exist_ok=True)
    with open(PROFILES_PATH, "w", encoding="utf-8") as f:
        json.dump(profs, f, indent=2)
    return p


def _slug(name: str) -> str:
    """A filesystem/url-safe id from a display name (lowercase, hyphenated)."""
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    return s or "user"


def add_profile(name: str) -> dict:
    """Create a NEW dispatcher with a unique id derived from `name`. Starts with no phone
    and no states (so it claims nothing until edited). Never reuses 'all'."""
    if not str(name).strip():
        raise ValueError("a name is required")
    profs = list_profiles()
    existing = {p.get("id") for p in profs} | {ALL_PROFILE_ID}
    base = _slug(name)
    pid, n = base, 2
    while pid in existing:
        pid, n = f"{base}-{n}", n + 1
    p = {"id": pid, "name": str(name).strip(), "phone": "", "states": []}
    profs.append(p)
    os.makedirs(PROFILES_DIR, exist_ok=True)
    with open(PROFILES_PATH, "w", encoding="utf-8") as f:
        json.dump(profs, f, indent=2)
    return p


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
# Washington DC ZIP prefixes: 200xx and 202xx–205xx are DC. 201xx is Virginia
# (Dulles), so it is deliberately NOT included. DC origins are sometimes written as
# "Washington" in the state column (which would read as WA), so we treat a DC ZIP as
# authoritative — DC loads route by ZIP, not by the ambiguous state text.
_DC_ZIP_PREFIXES = ("200", "202", "203", "204", "205")


def _is_dc_zip(zip_code) -> bool:
    z = str(zip_code or "").strip()
    return len(z) >= 3 and z[:3] in _DC_ZIP_PREFIXES


def _norm_state(s) -> str:
    """Normalize a state (full name or code) to its 2-letter code, uppercased."""
    import sd_api
    return sd_api._state(str(s or "").strip()).upper()


def row_pickup_state(row) -> str:
    """The row's effective PICKUP region for filtering. A DC ZIP wins over the state
    column, so a DC load goes to whoever covers 'DC' (Kelly) even if its origin state
    was typed as 'Washington'."""
    if _is_dc_zip(row.get("pickup_zip")):
        return "DC"
    return _norm_state(row.get("pickup_state"))


def allowed_states(profile: dict | None) -> set:
    # The ALL profile is unfiltered by design — never apply a state cap to it, even if a
    # `states` list somehow got saved on it. An empty set means filter_rows keeps every row.
    if (profile or {}).get("id") == ALL_PROFILE_ID:
        return set()
    return {_norm_state(s) for s in (profile or {}).get("states", []) if str(s).strip()}


def filter_rows(rows: list, profile: dict | None):
    """Keep only the rows whose PICKUP region the profile covers. An empty `states`
    list (not configured yet) means keep everything, so the app works until the
    profiles are filled in. DC is matched by ZIP (see row_pickup_state), so a DC load
    routes to the DC owner regardless of how its state column reads."""
    want = allowed_states(profile)
    if not want:
        return list(rows)
    return [r for r in rows if row_pickup_state(r) in want]
