"""
SAFE, READ-ONLY discovery of the SuperDispatch terminal *write* request.

Attaches to the shared logged-in Chrome (same CDP profile as the scraper), opens
the /terminals page, and intercepts every call to the internal terminals API:

  * GET            -> let through (the page + edit form must load normally)
  * POST/PUT/PATCH/DELETE -> CAPTURE the method/url/headers/body, then ABORT it
                     so the request NEVER reaches SuperDispatch. Nothing is written.

You then edit ONE terminal in the Chrome window and click Save. The save will
visibly FAIL in the UI — that's expected and means we caught it. Each captured
write is appended to output/terminals_write_capture.jsonl and dumped at the end.

Run:  python capture_terminal_write.py            # 5 min capture window
      python capture_terminal_write.py --seconds 600
"""
from __future__ import annotations
import argparse, json, os, time

import auth, config, paths
import sd_login

OUT_DIR = os.path.dirname(paths.output_path("terminals_discover", ".keep"))
JSONL = os.path.join(OUT_DIR, "terminals_write_capture.jsonl")

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Only intercept the terminals surface of the internal API — leave everything else alone.
MATCH = "**/internal/terminals/**"
MATCH2 = "**/internal/terminals?**"
MATCH3 = "**/internal/terminals"


def main(seconds: int) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    captured: list[dict] = []
    seen = {"get": 0}

    with auth.browser_context() as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        st = sd_login.ensure_session(page)
        if st != sd_login.LOGIN_OK:
            raise RuntimeError(f"SuperDispatch not logged in ({st}). Run sd_login.py first.")

        def handler(route):
            req = route.request
            m = (req.method or "").upper()
            if m in WRITE_METHODS:
                body = None
                try:
                    body = req.post_data
                except Exception:
                    body = None
                parsed = None
                if body:
                    try:
                        parsed = json.loads(body)
                    except Exception:
                        parsed = None
                rec = {
                    "ts": time.time(),
                    "method": m,
                    "url": req.url,
                    "resource_type": req.resource_type,
                    "headers": {k: v for k, v in (req.headers or {}).items()
                                if k.lower() in ("content-type", "authorization", "accept")},
                    "post_data": body,
                    "post_json": parsed,
                }
                captured.append(rec)
                try:
                    with open(JSONL, "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                except OSError:
                    pass
                auth_hdr = (req.headers or {}).get("authorization", "")
                print(f"\n*** CAPTURED WRITE #{len(captured)} (ABORTED — not sent to SD) ***")
                print(f"    {m} {req.url}")
                print(f"    auth: {'yes' if auth_hdr else 'no'}  body: {body!r}")
                route.abort()          # <-- the write NEVER reaches SuperDispatch
            else:
                seen["get"] += 1       # proves interception is live for this surface
                route.continue_()      # GETs (list, detail, contacts) pass through

        ctx.route(MATCH, handler)
        ctx.route(MATCH2, handler)
        ctx.route(MATCH3, handler)

        if "login" in (page.url or "").lower():
            page.goto(config.SD_WEB_BASE + "/terminals")
        else:
            page.goto(config.SD_WEB_BASE + "/terminals")
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass

        print("=" * 70)
        if seen["get"] == 0:
            print("WARNING: no GET to the terminals API was intercepted during load.")
            print("Interception may NOT be active — do NOT trust the abort. Reload the")
            print("/terminals page; if this persists, stop and tell Claude before saving.")
        else:
            print(f"SAFE: interception is LIVE ({seen['get']} GET(s) routed through the "
                  "handler). Any Save will be aborted before it reaches SuperDispatch.")
        print("READY. In the Chrome window that's open:")
        print("  1) Click a terminal to open it / hit Edit.")
        print("  2) Change a field (e.g. tweak the address or notes).")
        print("  3) Click SAVE.  The save will show an ERROR — that's expected;")
        print("     it means we intercepted the request before it hit SuperDispatch.")
        print(f"Listening for {seconds}s. Captures append to {JSONL}")
        print("=" * 70, flush=True)

        deadline = time.time() + seconds
        last = 0
        while time.time() < deadline:
            page.wait_for_timeout(1000)
            if len(captured) != last:
                last = len(captured)
                # keep a short grace window after the first capture for follow-up
                # requests (e.g. a separate /contacts call), then stop early.
                grace_end = time.time() + 12
                while time.time() < grace_end:
                    page.wait_for_timeout(1000)
                break

    print("\n" + "=" * 70)
    print(f"Done. {len(captured)} write request(s) captured (none sent to SD).")
    for i, r in enumerate(captured, 1):
        print(f"\n[{i}] {r['method']} {r['url']}")
        if r.get("post_json") is not None:
            print(json.dumps(r["post_json"], indent=2, ensure_ascii=False))
        elif r.get("post_data"):
            print(r["post_data"])
    if captured:
        print(f"\nFull capture: {JSONL}")
    else:
        print("No write captured — did the Save button get clicked?")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=300, help="capture window length")
    a = ap.parse_args()
    main(seconds=a.seconds)
