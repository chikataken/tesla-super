"""
Scrape every SuperDispatch terminal into the local cache (terminals_db).

Piggybacks the shared logged-in Chrome (auth.browser_context — same CDP profile as
the BOL tools). It does NOT scrape the DOM: the /terminals React page is backed by
a clean internal JSON API, so we capture the app's bearer token once and page the
API directly — far more robust than driving a virtualized list.

API (api.shipper.superdispatch.com/internal/terminals), all Bearer-authed:
  list:     GET ?size=1000            -> every terminal (id, guid, name, address,
                                          city, state, zip, phone)   [one call, 856 rows]
  detail:   GET /guid/<guid>          -> adds `notes` (the carrier note we want)
  contacts: GET /<id>/contacts        -> primary contact (name + phone)

Each terminal is upserted the moment it's enriched, so an interrupted run leaves a
valid, resumable cache (re-run with --resume to skip terminals already stored).

Run:  python terminals_scrape.py            # full refresh
      python terminals_scrape.py --resume   # skip terminals already in the cache
      python terminals_scrape.py --limit 50 # first 50 (smoke test)
"""
from __future__ import annotations
import argparse, time

import auth, config
import terminals_db as tdb

API = "https://api.shipper.superdispatch.com/internal/terminals"

# In-page: fetch list page with the captured bearer token.
_LIST_JS = """
async ([url, auth]) => {
  const r = await fetch(url, {headers: {Authorization: auth}});
  const j = await r.json();
  return {status: r.status, objects: (j?.data?.objects)||[],
          pagination: j?.data?.pagination || null};
}
"""

# In-page: for a BATCH of terminals, fetch detail (notes) + contacts in parallel.
_ENRICH_JS = """
async ([items, auth, base]) => {
  const one = async (it) => {
    try {
      const [d, c] = await Promise.all([
        fetch(`${base}/guid/${it.guid}`, {headers: {Authorization: auth}}).then(r => r.json()),
        fetch(`${base}/${it.id}/contacts`, {headers: {Authorization: auth}}).then(r => r.json()),
      ]);
      return {guid: it.guid, status: 200,
              notes: d?.data?.object?.notes ?? null,
              contacts: (c?.data?.objects) || []};
    } catch (e) { return {guid: it.guid, status: 0, error: String(e)}; }
  };
  return await Promise.all(items.map(one));
}
"""


def _primary_contact(contacts: list) -> tuple[str, str]:
    """Pick the primary contact (else the first); return (name, best phone)."""
    if not contacts:
        return "", ""
    c = next((x for x in contacts if x.get("is_primary")), contacts[0])
    name = (c.get("name") or "").strip()
    phone = (c.get("phone") or c.get("mobile_phone") or "").strip()
    return name, phone


def _capture_token(page) -> str | None:
    """Reload /terminals and grab the Authorization header the app sends to the API.
    Used at start and to refresh an expired token mid-run."""
    tok = {"v": None}
    def on_request(req):
        if "internal/terminals" in req.url and not tok["v"]:
            a = (req.headers or {}).get("authorization")
            if a:
                tok["v"] = a
    page.on("request", on_request)
    page.goto(config.SD_WEB_BASE + "/terminals")
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    for _ in range(20):
        if tok["v"]:
            break
        time.sleep(0.5)
    page.remove_listener("request", on_request)
    return tok["v"]


def main(limit: int | None = None, resume: bool = False) -> None:
    tdb.init_db()
    have = set()
    if resume:
        have = {t["sd_id"] for t in tdb.all_terminals()}
        print(f"resume: {len(have)} terminals already cached — will skip those")

    with auth.browser_context() as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        # Auto-log-in from Vaultwarden if the shared Chrome is logged out (careful, captcha-aware).
        import sd_login
        st = sd_login.ensure_session(page)
        if st != sd_login.LOGIN_OK:
            raise RuntimeError(
                f"SuperDispatch is logged out and auto-login could not complete ({st}). "
                + ("Solve the captcha / enter the 2FA code in the shared Chrome, then retry. "
                   if st in sd_login.HUMAN_NEEDED else "")
                + "Or run `python sd_login.py` once to log in manually.")
        if "login" in (page.url or "").lower():
            page.goto(config.SD_WEB_BASE + "/terminals")
        token = _capture_token(page)
        if not token:
            raise RuntimeError(
                "Couldn't capture the SuperDispatch API token — is the shared Chrome "
                f"logged into {config.SD_WEB_BASE}? Run sd_login.py once, then retry.")

        # 1) Whole list in one call.
        res = page.evaluate(_LIST_JS, [f"{API}?size=1000", token])
        objects = res.get("objects") or []
        total = (res.get("pagination") or {}).get("total_objects", len(objects))
        print(f"list: {len(objects)} terminals (API total={total})")
        if limit:
            objects = objects[:limit]

        todo = [o for o in objects if o.get("guid") not in have]
        print(f"enriching {len(todo)} terminals (skipping {len(objects)-len(todo)} cached)")
        tdb.set_scrape_progress(started=True, total_seen=0)

        # 2) Enrich (notes + primary contact) in batches, upserting as we go.
        BATCH = 8
        done = 0
        for i in range(0, len(todo), BATCH):
            batch = todo[i:i + BATCH]
            items = [{"guid": o["guid"], "id": o["id"]} for o in batch]
            try:
                enriched = page.evaluate(_ENRICH_JS, [items, token, API])
            except Exception as e:
                print(f"  batch {i//BATCH} error: {e} — refreshing token")
                token = _capture_token(page) or token
                enriched = page.evaluate(_ENRICH_JS, [items, token, API])
            by_guid = {e["guid"]: e for e in enriched}

            # If the whole batch 401'd (token expired), refresh once and redo.
            if all((by_guid.get(o["guid"], {}).get("status") == 0) for o in batch):
                token = _capture_token(page) or token
                enriched = page.evaluate(_ENRICH_JS, [items, token, API])
                by_guid = {e["guid"]: e for e in enriched}

            with tdb.connect() as conn:
                for o in batch:
                    e = by_guid.get(o["guid"], {})
                    name, phone = _primary_contact(e.get("contacts") or [])
                    rec = {
                        "sd_id": o["guid"],
                        "name": o.get("name") or "",
                        "address": o.get("address") or "",
                        "city": o.get("city") or "",
                        "state": o.get("state") or "",
                        "zip": o.get("zip") or "",
                        "contact_name": name,
                        "contact_phone": phone or (o.get("phone") or ""),
                        "carrier_notes": e.get("notes") or "",
                        "raw": {**o, "notes": e.get("notes"), "contacts": e.get("contacts")},
                    }
                    tdb.upsert_terminal(rec, conn=conn)
                conn.commit()
            done += len(batch)
            tdb.set_scrape_progress(total_seen=len(have) + done)
            print(f"  {done}/{len(todo)} enriched & saved")

        tdb.set_scrape_progress(finished=True, total_seen=tdb.count())
    # Re-link learned (bol) terminals to originals — first the strict exact-unique-address
    # pass, then the reasoned pass (same-address→name disambiguation + same-number fuzzy).
    link = tdb.link_learned_by_address()
    smart = tdb.link_learned_smart()
    print(f"done. cache now holds {tdb.count()} terminals -> {tdb.DB_PATH}")
    print(f"address-links: {link['linked']} exact, {smart['total']} reasoned "
          f"({smart['linked_addr_multi']} same-addr/name, {smart['linked_zip_fuzzy']} fuzzy)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="only first N terminals (smoke test)")
    ap.add_argument("--resume", action="store_true", help="skip terminals already cached")
    a = ap.parse_args()
    main(limit=a.limit, resume=a.resume)
