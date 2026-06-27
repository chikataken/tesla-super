"""
Push locally-edited terminals BACK to SuperDispatch — the write half of the daily sync.

Mirror image of terminals_scrape: piggybacks the shared logged-in Chrome, auto-relogs-in
from Vaultwarden if needed (sd_login.ensure_session), captures the app's bearer token, and
PUTs each change to the internal API. Both write contracts were captured LIVE from the app
(capture_terminal_write.py), never guessed:

  TERMINAL fields  PUT .../internal/terminals/<id>
                   body = full terminal object (id, guid, name, address, city, state, zip,
                          notes, phone, business_type, custom_external_id)
  CONTACT  fields  PUT .../internal/terminals/<id>/contacts/<contact_id>
                   body = full contact object (id, guid, name, is_primary, phone,
                          mobile_phone, email, title, phone_extension)

We push ONLY rows hand-edited in the Terminals UI (edited_at IS NOT NULL) AND originating on
SuperDispatch (source='sd'). The numeric ids, business_type, and the untouched parts of each
object come from the stored raw_json (which already holds the full contacts array); the
edited values come from our columns. One edit can produce TWO PUTs (terminal + contact).

LIMITATION: editing the contact on a terminal that has NO contact yet would be a CREATE
(POST), an endpoint we haven't captured — reported as a skip, never guessed. edited_at is
cleared only when EVERY operation for a row succeeds, so a partial failure stays pending and
the pull won't clobber it.

Run:  python terminals_push.py            # DRY RUN: show what would be pushed, send NOTHING
      python terminals_push.py --apply     # actually PUT the changes to SuperDispatch
"""
from __future__ import annotations
import argparse, json, time
from collections import defaultdict

import auth, config
import terminals_db as tdb
import terminals_scrape          # reuse API constant + token capture (same contract)

# Editable terminal columns -> SD body key. Contacts handled separately (below).
TERMINAL_FIELDS = [("name", "name"), ("address", "address"), ("city", "city"),
                   ("state", "state"), ("zip", "zip"), ("carrier_notes", "notes")]
# Exact key set the captured contact PUT used — send these and nothing else.
CONTACT_KEYS = ["name", "email", "is_primary", "title", "phone_extension",
                "phone", "mobile_phone", "guid", "id"]

# In-page: PUT a batch of {key, url, body} with the captured bearer token. Each item carries
# its own absolute URL so terminal and contact PUTs go through the same path.
_PUT_JS = """
async ([items, auth]) => {
  const one = async (it) => {
    try {
      const r = await fetch(it.url, {
        method: 'PUT',
        headers: {Authorization: auth, 'Content-Type': 'application/json'},
        body: JSON.stringify(it.body),
      });
      let text = '';
      try { text = await r.text(); } catch (e) {}
      return {key: it.key, status: r.status, ok: r.ok, body: (text || '').slice(0, 300)};
    } catch (e) { return {key: it.key, status: 0, error: String(e)}; }
  };
  return await Promise.all(items.map(one));
}
"""


def _primary_contact(raw: dict):
    cs = raw.get("contacts") or []
    if not cs:
        return None
    return next((c for c in cs if c.get("is_primary")), cs[0])


def _contact_phone(c: dict) -> str:
    """The single phone we surface for a contact — same precedence the scraper used."""
    return ((c.get("phone") or "").strip() or (c.get("mobile_phone") or "").strip())


def _phone_field(c: dict) -> str:
    """Which field a contact's phone lives in, so we write the edit back to the SAME one."""
    if (c.get("phone") or "").strip():
        return "phone"
    if (c.get("mobile_phone") or "").strip():
        return "mobile_phone"
    return "phone"


def _classify():
    """Turn every edited row into concrete PUT operations. Returns (ops, skips):
      ops   — list of {row, kind:'terminal'|'contact', url, body, diff, key}
      skips — list of (row, reason) for edits we can't push (and why)."""
    ops, skips = [], []
    for r in tdb.all_terminals():
        if not r.get("edited_at"):
            continue
        if (r.get("source") or "sd") != "sd":
            skips.append((r, "learned/added terminal — no SuperDispatch record"))
            continue
        try:
            raw = json.loads(r.get("raw_json") or "{}")
        except Exception:
            raw = {}
        tid = raw.get("id")
        if tid in (None, ""):
            skips.append((r, "no numeric id in raw_json — re-scrape this terminal first"))
            continue

        # --- terminal-field changes ---
        tdiff = []
        for col, key in TERMINAL_FIELDS:
            if str(r.get(col) or "").strip() != str(raw.get(key) or "").strip():
                tdiff.append((key, raw.get(key) or "", r.get(col) or ""))
        if tdiff:
            body = {
                "id": tid, "guid": r.get("sd_id"),
                "business_type": raw.get("business_type") or "BUSINESS",
                "custom_external_id": raw.get("custom_external_id"),
                "name": r.get("name") or "", "address": r.get("address") or "",
                "city": r.get("city") or "", "state": r.get("state") or "",
                "zip": r.get("zip") or "", "notes": (r.get("carrier_notes") or "") or None,
                "phone": raw.get("phone"),
            }
            ops.append({"row": r, "kind": "terminal", "diff": tdiff, "body": body,
                        "url": f"{terminals_scrape.API}/{tid}", "key": f"{r['sd_id']}|terminal"})

        # --- contact (name / phone) changes ---
        prim = _primary_contact(raw)
        cn_new = (r.get("contact_name") or "").strip()
        cp_new = (r.get("contact_phone") or "").strip()
        cn_old = ((prim.get("name") if prim else "") or "").strip()
        cp_old = (_contact_phone(prim) if prim else "")
        cdiff = []
        if cn_new != cn_old:
            cdiff.append(("contact_name", cn_old, cn_new))
        if cp_new != cp_old:
            cdiff.append(("contact_phone", cp_old, cp_new))
        if cdiff:
            if prim and prim.get("id"):
                cbody = {k: prim.get(k) for k in CONTACT_KEYS}
                cbody["name"] = cn_new
                cbody[_phone_field(prim)] = cp_new or None
                if cbody.get("is_primary") is None:
                    cbody["is_primary"] = True
                ops.append({"row": r, "kind": "contact", "diff": cdiff, "body": cbody,
                            "url": f"{terminals_scrape.API}/{tid}/contacts/{prim['id']}",
                            "key": f"{r['sd_id']}|contact"})
            else:
                skips.append((r, "contact edit but terminal has no existing contact "
                                 "(create/POST not captured yet)"))
    return ops, skips


def _apply_success(sd_id: str, succeeded_ops: list, all_ok: bool) -> None:
    """After PUTs: merge each SUCCEEDED op's new values into raw_json (new SD baseline) and,
    only if EVERY op for the row succeeded, clear edited_at (fully in sync)."""
    with tdb.connect() as conn:
        row = conn.execute("SELECT raw_json FROM terminals WHERE sd_id=?", (sd_id,)).fetchone()
        if not row:
            return
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except Exception:
            raw = {}
        for op in succeeded_ops:
            if op["kind"] == "terminal":
                for k in ("name", "address", "city", "state", "zip", "notes"):
                    raw[k] = op["body"].get(k)
            elif op["kind"] == "contact":
                cs = raw.get("contacts") or []
                cid = op["body"].get("id")
                replaced = False
                for i, c in enumerate(cs):
                    if c.get("id") == cid:
                        cs[i] = op["body"]; replaced = True; break
                if not replaced:
                    cs.append(op["body"])
                raw["contacts"] = cs
        sets, vals = "raw_json=?, updated_at=?", [json.dumps(raw, ensure_ascii=False), time.time()]
        if all_ok:
            sets += ", edited_at=NULL"
        conn.execute(f"UPDATE terminals SET {sets} WHERE sd_id=?", vals + [sd_id])
        conn.commit()


def _fmt(v) -> str:
    return repr("" if v is None else v)


def main(apply: bool = False) -> dict:
    tdb.init_db()
    ops, skips = _classify()
    rows = {op["row"]["sd_id"]: op["row"] for op in ops}
    n_term = sum(1 for o in ops if o["kind"] == "terminal")
    n_contact = sum(1 for o in ops if o["kind"] == "contact")

    print(f"push: {len(rows)} terminal(s) to update "
          f"({n_term} terminal-field PUT, {n_contact} contact PUT), {len(skips)} skipped")
    for r, reason in skips:
        print(f"  SKIP ({reason}): {r.get('name')}")

    if not ops:
        print("nothing to push." + ("" if apply else "  (dry run)"))
        return {"pushed_rows": 0, "failed": 0, "skipped": len(skips)}

    # Show exactly what changes, grouped by terminal.
    by_row = defaultdict(list)
    for op in ops:
        by_row[op["row"]["sd_id"]].append(op)
    for sd_id, lst in by_row.items():
        print(f"\n• {rows[sd_id].get('name')}  [{sd_id[:18]}]")
        for op in lst:
            for field, old, new in op["diff"]:
                print(f"    [{op['kind']}] {field}: {_fmt(old)} -> {_fmt(new)}")

    if not apply:
        print(f"\nDRY RUN — nothing sent. Re-run with --apply to PUT {len(ops)} change(s) "
              f"across {len(rows)} terminal(s).")
        return {"pushed_rows": 0, "failed": 0, "would_push": len(ops), "skipped": len(skips)}

    if config.TEST_MODE:                              # the test site never writes to SD
        print("\nTEST MODE — SuperDispatch writes are disabled. Nothing pushed.")
        return {"pushed_rows": 0, "failed": 0, "test_mode": True, "skipped": len(skips)}

    # --- apply: open the shared Chrome, (re)login, capture token, PUT each op ---
    with auth.browser_context() as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
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
        token = terminals_scrape._capture_token(page)
        if not token:
            raise RuntimeError("Couldn't capture the SuperDispatch API token — is the shared "
                               f"Chrome logged into {config.SD_WEB_BASE}?")

        items = [{"key": op["key"], "url": op["url"], "body": op["body"]} for op in ops]
        results = page.evaluate(_PUT_JS, [items, token])
        if results and all((x.get("status") in (0, 401)) for x in results):
            token = terminals_scrape._capture_token(page) or token
            results = page.evaluate(_PUT_JS, [items, token])
        by_key = {x["key"]: x for x in results}

    pushed_rows = failed = 0
    for sd_id, lst in by_row.items():
        outcomes = [(op, by_key.get(op["key"], {})) for op in lst]
        all_ok = all(res.get("ok") for _, res in outcomes)
        _apply_success(sd_id, [op for op, res in outcomes if res.get("ok")], all_ok)
        for op, res in outcomes:
            tag = "PUSHED" if res.get("ok") else "FAILED"
            extra = "" if res.get("ok") else f" -> {res.get('error') or res.get('body','')}"
            print(f"  {tag} [{op['kind']}] {rows[sd_id].get('name')}  (HTTP {res.get('status')}){extra}")
        pushed_rows += all_ok
        failed += sum(1 for _, res in outcomes if not res.get("ok"))

    print(f"\npush done: {pushed_rows}/{len(rows)} terminal(s) fully synced, {failed} op(s) failed.")
    return {"pushed_rows": pushed_rows, "failed": failed, "skipped": len(skips)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually PUT changes (default: dry run)")
    a = ap.parse_args()
    main(apply=a.apply)
