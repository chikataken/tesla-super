"""
Local SuperDispatch *terminals* cache — the source of truth for venue info that
shipment posting prefers over the Tesla BOL.

WHY A DB (not a JSON file): the scrape walks ~850 terminals one-by-one and is
long and interruptible (logout, captcha, a crash at #600). SQLite lets the
scraper UPSERT each terminal the instant it reads it, keyed on the SD terminal
id — a partial run leaves a valid, queryable file and resumes cleanly. A JSON
array would force rewrite-the-whole-file-per-terminal or hold-all-in-RAM-then-
dump (lose everything on a mid-run crash). Posting reads this table while a
refresh scrape writes it; WAL makes that safe. Same pattern proven in the
sibling direct-pickup-checks tool. sqlite3 is stdlib, so nothing new ships.

LOOKUP CONTRACT (see terminals_lookup.resolve_venue): a posting venue is matched
to a terminal by EXACT normalized name (BOL `location` == SD terminal `name`).
On a hit we replace the whole venue + carrier notes with the terminal's data; on
a miss the caller keeps the current Tesla-BOL logic. `name_norm` + `raw_json` are
stored so a future fuzzy/Claude name-linking layer can run without re-scraping.
"""
from __future__ import annotations
import json
import re
import sqlite3
import time
from typing import Any, Iterable, Optional

import paths

DB_PATH = paths.data_path("terminals.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS terminals (
    sd_id         TEXT PRIMARY KEY,        -- SuperDispatch terminal id (stable key)
    name          TEXT,                    -- verbatim SD terminal name (e.g. NA-US-IN-Indianapolis)
    name_norm     TEXT,                    -- normalized name -> exact-match join key
    address       TEXT,
    city          TEXT,
    state         TEXT,                    -- 2-letter where SD gives it
    zip           TEXT,
    zip5          TEXT,                     -- first 5 digits, for geo narrowing
    contact_name  TEXT,
    contact_phone TEXT,
    carrier_notes TEXT,                    -- default pickup/delivery notes on the terminal
    source        TEXT NOT NULL DEFAULT 'sd',  -- 'sd' = scraped from SuperDispatch | 'bol' = learned from a Tesla BOL
    linked_sd_id  TEXT,                    -- a learned (bol) terminal whose EXACT address matches a
                                           -- scraped (sd) terminal points here -> lookups use that original
    raw_json      TEXT,                    -- full scraped blob (re-parse later w/o re-scraping)
    scraped_at    REAL,
    updated_at    REAL
);
CREATE INDEX IF NOT EXISTS ix_terminals_name ON terminals(name_norm);
CREATE INDEX IF NOT EXISTS ix_terminals_zip5 ON terminals(zip5);
CREATE INDEX IF NOT EXISTS ix_terminals_state ON terminals(state);

-- Single-row scrape cursor so a refresh resumes where it stopped.
CREATE TABLE IF NOT EXISTS scrape_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    last_page   INTEGER NOT NULL DEFAULT 0,   -- highest terminals-list page fully scraped
    total_seen  INTEGER NOT NULL DEFAULT 0,
    started_at  REAL,
    finished_at REAL,
    updated_at  REAL
);
INSERT OR IGNORE INTO scrape_state (id, updated_at) VALUES (1, 0);

-- Running, de-duplicated list of BOL terminal names that DIDN'T cleanly resolve to a
-- single SD terminal during posting — so the gaps in the cache are visible. Keyed on
-- the normalized name (dedup); `hits` counts how often it recurred. reason:
-- 'not_found' (no SD terminal) | 'ambiguous' (duplicated name).
CREATE TABLE IF NOT EXISTS unmatched_terminals (
    name_norm   TEXT PRIMARY KEY,
    name        TEXT,                          -- the verbatim BOL location string
    reason      TEXT,
    hits        INTEGER NOT NULL DEFAULT 0,
    first_seen  REAL,
    last_seen   REAL
);
"""


def normalize_name(name: str) -> str:
    """Canonical form for the exact-match join: trim, collapse whitespace, casefold.
    Punctuation is preserved (Tesla codes like 'NA-US-IN-Indianapolis' rely on the
    hyphens) — only surrounding/duplicate whitespace and case vary across the UI."""
    return re.sub(r"\s+", " ", (name or "").strip()).casefold()


def _zip5(z: str) -> str:
    m = re.search(r"\d{5}", z or "")
    return m.group(0) if m else ""


_ADDR_ABBR = {
    r'\bboulevard\b': 'blvd', r'\bstreet\b': 'st', r'\bavenue\b': 'ave', r'\bdrive\b': 'dr',
    r'\broad\b': 'rd', r'\bhighway\b': 'hwy', r'\blane\b': 'ln', r'\bcourt\b': 'ct',
    r'\bparkway\b': 'pkwy', r'\bsuite\b': 'ste', r'\bnorth\b': 'n', r'\bsouth\b': 's',
    r'\beast\b': 'e', r'\bwest\b': 'w', r'\bplace\b': 'pl',
}


def normalize_address(a: str) -> str:
    """Canonical street address for the EXACT-match link: lowercase, expand common
    abbreviations (Blvd/Boulevard…), strip punctuation, collapse whitespace. Returns ''
    for blank — never link on an empty address."""
    s = (a or "").lower()
    for pat, repl in _ADDR_ABBR.items():
        s = re.sub(pat, repl, s)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()


def connect() -> sqlite3.Connection:
    """WAL + busy timeout so a refresh scrape and a posting read don't collide."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


_initialized = False


def init_db() -> None:
    """Create the schema if absent. Safe to call on every process start."""
    global _initialized
    with connect() as conn:
        conn.executescript(_SCHEMA)
        # Migrations: add columns to a terminals table created before they existed.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(terminals)")}
        if "source" not in cols:
            conn.execute("ALTER TABLE terminals ADD COLUMN source TEXT NOT NULL DEFAULT 'sd'")
        if "linked_sd_id" not in cols:
            conn.execute("ALTER TABLE terminals ADD COLUMN linked_sd_id TEXT")
        conn.commit()
    _initialized = True


def _ensure() -> None:
    """Lazily apply the schema once per process, so the posting path (which never runs
    the scraper) still has every table — including unmatched_terminals — even on an
    older cache file. CREATE TABLE IF NOT EXISTS makes this a cheap no-op."""
    if not _initialized:
        init_db()


# --------------------------- writes (scraper) ---------------------------

def upsert_terminal(t: dict, *, conn: Optional[sqlite3.Connection] = None) -> None:
    """Insert/replace one terminal. Keyed on sd_id so re-scraping is idempotent and
    a partial run can resume without dupes. `t` carries the SD-venue fields plus an
    `sd_id`; anything extra is preserved verbatim in raw_json for later re-parsing."""
    own = conn is None
    conn = conn or connect()
    try:
        now = time.time()
        name = t.get("name", "") or ""
        conn.execute(
            """INSERT INTO terminals
                 (sd_id, name, name_norm, address, city, state, zip, zip5,
                  contact_name, contact_phone, carrier_notes, source, raw_json,
                  scraped_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(sd_id) DO UPDATE SET
                 name=excluded.name, name_norm=excluded.name_norm,
                 address=excluded.address, city=excluded.city, state=excluded.state,
                 zip=excluded.zip, zip5=excluded.zip5,
                 contact_name=excluded.contact_name, contact_phone=excluded.contact_phone,
                 carrier_notes=excluded.carrier_notes, source=excluded.source,
                 raw_json=excluded.raw_json,
                 scraped_at=excluded.scraped_at, updated_at=excluded.updated_at""",
            (
                t.get("sd_id", "") or "",
                name,
                normalize_name(name),
                t.get("address", "") or "",
                t.get("city", "") or "",
                t.get("state", "") or "",
                t.get("zip", "") or "",
                _zip5(t.get("zip", "") or ""),
                t.get("contact_name", "") or "",
                t.get("contact_phone", "") or "",
                t.get("carrier_notes", "") or "",
                t.get("source", "sd") or "sd",
                json.dumps(t.get("raw", t), ensure_ascii=False),
                now,
                now,
            ),
        )
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def learn_terminal(name: str, *, address: str = "", city: str = "", state: str = "",
                   zip: str = "", contact_name: str = "", contact_phone: str = "",
                   carrier_notes: str = "", raw: Optional[dict] = None) -> bool:
    """Save a terminal LEARNED from a Tesla BOL (source='bol') so it's known next time
    and we can skip the portal. No-op (returns False) when a terminal with this name
    already exists — scraped OR learned — so we only fill genuine gaps and never clobber
    SD data or create a duplicate name. sd_id is synthetic ('bol:<name_norm>')."""
    nn = normalize_name(name)
    if not nn:
        return False
    _ensure()
    if rows_by_name(name):                 # already known (scraped or previously learned)
        return False
    upsert_terminal({
        "sd_id": f"bol:{nn}", "name": name,
        "address": address, "city": city, "state": state, "zip": zip,
        "contact_name": contact_name, "contact_phone": contact_phone,
        "carrier_notes": carrier_notes, "source": "bol",
        "raw": raw or {"learned_from": "tesla_bol"},
    })
    # Auto-link to an original scraped terminal at the EXACT same address, so this BOL
    # name resolves straight to the original next time (no separate 'added' entry in use).
    _link_one(f"bol:{nn}", address)
    return True


def _sd_address_index() -> dict:
    """{normalized_address: sd_id} for scraped (sd) terminals at a UNIQUE address. An
    address shared by 2+ scraped terminals is omitted — ambiguous, so we don't link it."""
    by_addr: dict[str, list[str]] = {}
    with connect() as conn:
        for r in conn.execute(
                "SELECT sd_id, address FROM terminals WHERE source='sd' AND address!=''"):
            by_addr.setdefault(normalize_address(r["address"]), []).append(r["sd_id"])
    return {a: ids[0] for a, ids in by_addr.items() if len(ids) == 1}


def _link_one(sd_id: str, address: str, index: Optional[dict] = None) -> bool:
    """Point one learned terminal at the original scraped terminal sharing its EXACT
    address (when that original is unambiguous). Returns True if a link was set."""
    na = normalize_address(address)
    if not na:
        return False
    idx = index if index is not None else _sd_address_index()
    target = idx.get(na)
    if not target or target == sd_id:
        return False
    with connect() as conn:
        conn.execute("UPDATE terminals SET linked_sd_id=? WHERE sd_id=?", (target, sd_id))
        conn.commit()
    return True


_NAME_STOP = {"na", "us", "the", "of", "tesla", "inc", "llc", "service", "center",
              "centers", "collision", "offsite", "trade", "dealer", "showroom"}


def _is_junk_phone(p: str) -> bool:
    """A placeholder/garbage phone, not a real one: blank, all-same-digit (111-111-1111,
    0000000000), or the 1234567890 sequence. Conservative — a real number is never flagged."""
    d = re.sub(r"\D", "", p or "")
    if not d:
        return True
    if len(set(d)) <= 1:
        return True
    return d[:10] in ("1234567890", "0123456789")


def _is_junk_contact(c: str) -> bool:
    """A garbage contact 'name' that's really a number/zip/place fragment, not a person or
    desk: blank, pure digits/punctuation, a bare ZIP, or an address fragment ('America, 85050')."""
    s = (c or "").strip()
    if not s:
        return True
    if re.fullmatch(r"[\d\s().,\-]+", s):                       # pure number / phone-ish
        return True
    if re.fullmatch(r"\d{5}(-\d{4})?", s):                      # a ZIP
        return True
    return bool(re.search(r"\b(united states|america|usa)\b", s.lower()))


def _best_field(orig: str, alias: str, is_junk) -> str:
    """Prefer the original's REAL value; if it's blank/junk, use the alias's real value;
    if BOTH are blank/junk, post nothing (a blank beats a fake placeholder)."""
    o, a = (orig or "").strip(), (alias or "").strip()
    if o and not is_junk(o):
        return o
    if a and not is_junk(a):
        return a
    return ""


def _name_tokens(name: str) -> set:
    """Meaningful tokens of a terminal name for disambiguation — drops the NA-US-XX-
    prefix and generic words, keeps the place-identifying ones (city, suburb, street)."""
    s = re.sub(r"^na[\s\-]*us[\s\-]*[a-z]{2}[\s\-]*", "", (name or "").lower())
    return {w for w in re.split(r"[^a-z0-9]+", s) if len(w) >= 3 and w not in _NAME_STOP}


def _name_overlap(a: str, b: str) -> float:
    ta, tb = _name_tokens(a), _name_tokens(b)
    return len(ta & tb) / max(1, min(len(ta), len(tb))) if (ta and tb) else 0.0


def _street_no(a: str) -> str:
    m = re.match(r"\s*(\d+)", a or "")
    return m.group(1) if m else ""


def link_learned_smart() -> dict:
    """Reasoned linking BEYOND exact-unique-address (link_learned_by_address). Two cases the
    strict linker can't do, with guards so it stays safe:
      - SAME exact address shared by 2+ originals -> disambiguate by name-token overlap
        (e.g. NA-US-CA-Miramar-Trade -> 'Miramar - Trade', not 'Tesla - San Diego Trade').
      - Slight address formatting drift at the SAME zip -> link only when the street NUMBER
        is identical and the rest is ~the same ('2535 Arden Wy' == '2535 Arden Way'), never
        a different building ('9800 …' vs '9585 …').
    Idempotent; only sets links on currently-unlinked learned terminals. Returns stats."""
    try:
        from rapidfuzz import fuzz
    except Exception:
        return {"error": "rapidfuzz not available", "linked": 0}
    _ensure()
    ts = all_terminals()
    sd = [t for t in ts if (t.get("source") or "sd") == "sd"]
    by_addr: dict[str, list] = {}
    by_zip: dict[str, list] = {}
    for t in sd:
        if t.get("address"):
            by_addr.setdefault(normalize_address(t["address"]), []).append(t)
        if t.get("zip"):
            by_zip.setdefault((t.get("zip") or "")[:5], []).append(t)

    linked_addr = linked_fuzzy = 0
    applied = []
    for b in ts:
        if b.get("source") != "bol" or b.get("linked_sd_id"):
            continue
        ba = normalize_address(b.get("address", ""))
        pick = None
        if ba and len(by_addr.get(ba, [])) >= 2:                 # same address, many originals
            cands = by_addr[ba]
            pick = max(cands, key=lambda c: (_name_overlap(b["name"], c["name"]),
                                             fuzz.token_sort_ratio(b["name"], c["name"])))
            kind = "addr-multi"
        elif ba:                                                 # zip + fuzzy address, same number
            best, bs = None, 0
            for c in by_zip.get((b.get("zip") or "")[:5], []):
                ca = normalize_address(c.get("address", ""))
                if _street_no(ba) and _street_no(ba) == _street_no(ca):
                    s = fuzz.token_sort_ratio(ba, ca)
                    if s > bs:
                        bs, best = s, c
            if best and bs >= 90:
                pick, kind = best, "zip-fuzzy"
        if not pick or pick.get("sd_id") == b.get("sd_id"):
            continue
        with connect() as conn:
            conn.execute("UPDATE terminals SET linked_sd_id=? WHERE sd_id=?",
                         (pick["sd_id"], b["sd_id"]))
            conn.commit()
        applied.append((b["name"], pick["name"], kind))
        linked_addr += kind == "addr-multi"
        linked_fuzzy += kind == "zip-fuzzy"
    return {"linked_addr_multi": linked_addr, "linked_zip_fuzzy": linked_fuzzy,
            "total": linked_addr + linked_fuzzy, "applied": applied}


def link_learned_by_address() -> dict:
    """Link every learned (bol) terminal to the original scraped (sd) terminal at its
    EXACT address (unambiguous matches only — slight deviations / shared addresses are
    left alone). Idempotent. Returns {linked, already, unmatched}."""
    _ensure()
    idx = _sd_address_index()
    linked = already = unmatched = 0
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT sd_id, address, linked_sd_id FROM terminals WHERE source='bol'")]
    for r in rows:
        target = idx.get(normalize_address(r.get("address", "")))
        if not target or target == r["sd_id"]:
            unmatched += 1
            continue
        if r.get("linked_sd_id") == target:
            already += 1
            continue
        with connect() as conn:
            conn.execute("UPDATE terminals SET linked_sd_id=? WHERE sd_id=?", (target, r["sd_id"]))
            conn.commit()
        linked += 1
    return {"linked": linked, "already": already, "unmatched": unmatched}


def set_scrape_progress(*, last_page: Optional[int] = None,
                        total_seen: Optional[int] = None,
                        started: bool = False, finished: bool = False) -> None:
    """Advance the resume cursor. Called after each list page is committed."""
    now = time.time()
    sets, vals = ["updated_at=?"], [now]
    if last_page is not None:
        sets.append("last_page=?"); vals.append(last_page)
    if total_seen is not None:
        sets.append("total_seen=?"); vals.append(total_seen)
    if started:
        sets.append("started_at=?"); vals.append(now); sets.append("finished_at=NULL")
    if finished:
        sets.append("finished_at=?"); vals.append(now)
    with connect() as conn:
        conn.execute(f"UPDATE scrape_state SET {', '.join(sets)} WHERE id=1", vals)
        conn.commit()


def get_scrape_state() -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM scrape_state WHERE id=1").fetchone()
    return dict(row) if row else {}


# --------------------------- reads (posting) ---------------------------

def rows_by_name(name: str) -> list[dict]:
    """All terminals whose normalized name matches. Usually 0 or 1, but ~10 names
    are shared by multiple terminals (e.g. 'tesla carson') — the caller decides what
    to do with an ambiguous (>1) match."""
    nn = normalize_name(name)
    if not nn:
        return []
    _ensure()
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM terminals WHERE name_norm=?", (nn,)).fetchall()]


def get_by_name(name: str) -> Optional[dict]:
    """Exact normalized-name match — the posting join. Returns the terminal only when
    it's UNAMBIGUOUS (exactly one match); None on no match OR a duplicated name, so an
    ambiguous name can't post the wrong terminal's address."""
    rows = rows_by_name(name)
    return rows[0] if len(rows) == 1 else None


def get_by_sd_id(sd_id: str) -> Optional[dict]:
    _ensure()
    with connect() as conn:
        row = conn.execute("SELECT * FROM terminals WHERE sd_id=?", (sd_id or "",)).fetchone()
    return dict(row) if row else None


def resolve_row(name: str) -> tuple[Optional[dict], Optional[str]]:
    """Exact-name lookup that FOLLOWS an address-link. Returns (row, kind):
      kind 'linked' — a learned name aliased (by exact address) to its original scraped
                      terminal; `row` is that ORIGINAL terminal.
      kind 'db'     — a direct match on an original scraped (sd) terminal.
      kind 'added'  — a learned (bol) terminal with no link (genuinely new).
    (None, None) on no/ambiguous match. This is the single source of truth both posting
    and the skip-the-portal gate use, so links apply everywhere."""
    row = get_by_name(name)
    if not row:
        return None, None
    link = row.get("linked_sd_id")
    if link:
        canon = get_by_sd_id(link)
        if canon:
            # Prefer the ORIGINAL's contact/phone, but if it's blank OR junk
            # (111-111-1111, a zip, etc.) use the learned BOL value instead — and if both
            # are junk, post nothing rather than a fake placeholder. Notes just fill blanks.
            merged = dict(canon)
            merged["contact_name"] = _best_field(canon.get("contact_name"),
                                                 row.get("contact_name"), _is_junk_contact)
            merged["contact_phone"] = _best_field(canon.get("contact_phone"),
                                                  row.get("contact_phone"), _is_junk_phone)
            if not (merged.get("carrier_notes") or "").strip() and (row.get("carrier_notes") or "").strip():
                merged["carrier_notes"] = row["carrier_notes"]
            return merged, "linked"
        # dangling link (canonical deleted) -> fall back to the alias row itself
    return row, ("added" if (row.get("source") or "sd") == "bol" else "db")


def all_terminals() -> list[dict]:
    """Every terminal — for the future in-memory fuzzy/Claude name-linking layer."""
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM terminals").fetchall()]


def count() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM terminals").fetchone()[0]


# --------------------------- unmatched-name list ---------------------------

# Plain-text mirror of the unmatched table, regenerated on each miss so the running
# list is openable without a DB tool.
MISS_FILE = paths.data_path("unmatched_terminals.txt")


def record_miss(name: str, reason: str = "not_found") -> None:
    """Append a BOL terminal name that didn't cleanly resolve, de-duplicated on the
    normalized name (bumps `hits` + `last_seen` instead of inserting a dup)."""
    nn = normalize_name(name)
    if not nn:
        return
    _ensure()
    now = time.time()
    with connect() as conn:
        conn.execute(
            """INSERT INTO unmatched_terminals (name_norm, name, reason, hits, first_seen, last_seen)
               VALUES (?,?,?,1,?,?)
               ON CONFLICT(name_norm) DO UPDATE SET
                 hits = hits + 1, last_seen = excluded.last_seen, reason = excluded.reason""",
            (nn, (name or "").strip(), reason, now, now))
        conn.commit()
        rows = conn.execute(
            "SELECT name, reason, hits FROM unmatched_terminals ORDER BY name COLLATE NOCASE").fetchall()
    try:
        with open(MISS_FILE, "w", encoding="utf-8") as f:
            f.write(f"# BOL terminal names with no clean SD match ({len(rows)} unique)\n")
            for r in rows:
                f.write(f"{r['name']}\t[{r['reason']}, seen {r['hits']}x]\n")
    except OSError:
        pass


def unmatched_list() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM unmatched_terminals ORDER BY name COLLATE NOCASE").fetchall()]
