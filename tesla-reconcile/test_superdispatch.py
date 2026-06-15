"""
Full workflow prototype.

Playwright scans the SuperDispatch Invoiced list, picks qualifying shipments
(skipping OK / Paid / Delivery confirmed / Damage claim / flagged), checks every
VIN on Tesla (Approved payment + Filed claims), then for clean orders opens the
online BOL and hands the delivery photos to Claude to verify (a) the delivery
ZIP matches and (b) the VIN is photographed on the vehicle. Finally it tags.

Flag logic:
  - any VIN with blank payment    -> no flag, leave for manual review
  - any VIN with a damage claim    -> 'Damage claim'
  - otherwise -> 'Delivery confirmed'  (+ 'No VIN photo' if no on-vehicle VIN
    photo is found; location mismatch is flagged for manual review)

Usage:
    python test_superdispatch.py                 # 1 shipment, full check
    python test_superdispatch.py --count 5
    python test_superdispatch.py --skip-vision   # skip the BOL/photo step
    python test_superdispatch.py --dry-run       # decide but don't write tags
"""
import argparse
import datetime as dt
import re

import config
import ocr
import superdispatch as sd
import tesla
import vision
import zipdist
from auth import browser_context

# Delivered-On window. START = the first day of the PREVIOUS month, computed at
# run time so it rolls forward automatically (June -> May 1; July -> June 1; …),
# rather than a fixed date. END = today minus 7 days (the lag that lets Tesla post
# payment before we check a recent delivery).
_TODAY = dt.date.today()
WINDOW_START = (_TODAY.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
WINDOW_END = _TODAY - dt.timedelta(days=7)

# --troubleshoot: print Claude's short reasoning whenever a VIN doesn't cleanly
# pass (not found / mismatch / confidence below this). Set in __main__.
TROUBLESHOOT = False
TROUBLESHOOT_CONF = 0.6


def _norm_vin(v):
    return re.sub(r"[^A-Z0-9]", "", (v or "").upper())


def _vin_match(read, expected):
    """Compare the VIN Claude READ off the car to the assigned VIN, in code (the
    model reads reliably but is wishy-washy on the yes/no). Returns:
      True   -> same vehicle (confirm, even if the model hedged its boolean)
      False  -> a clearly different VIN (wrong car)
      None   -> too little / too ambiguous to decide; defer to the model.
    Tesla VINs share a long prefix, so the distinguishing part is the last 6
    (the serial); we weight that and tolerate a little glare/OCR noise."""
    r, e = _norm_vin(read), _norm_vin(expected)
    if len(e) < 11:
        return None
    # A confident match can come from the serial alone; a MISMATCH requires a
    # near-complete read (don't call "wrong car" off a half-read prefix).
    if len(r) >= 11 and r[-6:] == e[-6:]:
        return True            # serial (last 6) matches -> same car, prefix noise OK
    if len(r) < 15:
        return None            # too partial to judge a mismatch -> defer to the model
    L = min(len(r), len(e))
    diffs = sum(1 for i in range(L) if r[i] != e[i]) + abs(len(r) - len(e))
    if diffs <= 2:
        return True            # near-identical overall (a glare slip or two)
    if diffs >= 4:
        return False           # clearly a different VIN
    return None


def _section_for_vin(sections, vin, idx, n):
    """The BOL delivery section belonging to this VIN (by VIN, then by order)."""
    nv = _norm_vin(vin)
    for s in sections:
        if _norm_vin(s.get("vin")) == nv:
            return s
    if len(sections) == n and idx < len(sections):
        return sections[idx]
    return None


def collect_qualifying(page, count, max_pages):
    found = []
    seen = set()                       # detail_urls already collected (dedupe across pages)
    for pageno in range(1, max_pages + 1):
        # OLDEST delivery first (ascending). The window can hold ~1,600 invoiced
        # orders (~80 pages of 20), so --max-pages must be high enough to reach the
        # recent, still-markable deliveries at the end — the default is 80. Earlier
        # pages are mostly already marked ("Delivery confirmed") and get skipped.
        page.goto(sd.invoiced_url(WINDOW_START, WINDOW_END, page=pageno, ascending=True))
        rows = sd.scrape_order_rows(page)
        if not rows:
            print(f"  page {pageno}: 0 rows -> stop")
            break
        new = [r for r in rows if r.detail_url not in seen]
        qualifying_new = 0
        for r in new:
            seen.add(r.detail_url)
            if not r.should_skip:
                found.append(r)
                qualifying_new += 1
                if count and len(found) >= count:   # count=0 -> no limit (all)
                    print(f"  page {pageno}: {len(rows)} rows, +{qualifying_new} "
                          f"qualifying -> reached count={count}")
                    return found
        print(f"  page {pageno}: {len(rows)} rows, {len(new)} new, "
              f"+{qualifying_new} qualifying (running total {len(found)})")
        if not new:
            print(f"  page {pageno}: no NEW orders — the list isn't paginating on "
                  f"the page param (virtualized list). Stopping.")
            break
    return found


def apply_tags(sd_page, row, edit_url, tags):
    """Clear-then-set: remove any existing tags and set exactly `tags` (always
    <=3, within SuperDispatch's cap). Skips the write only if the shipment already
    carries exactly this set."""
    want = {t.strip().lower() for t in tags}
    have = {t.strip().lower() for t in (row.tags or [])}
    if want == have:
        print(f"  -> tags already exactly {tags}, nothing to write")
        return
    sd.add_tags(sd_page, edit_url, tags)          # removes existing, sets these
    after = sd.read_edit_tags(sd_page, edit_url)
    print(f"  -> set {tags}  (tags now: {after})")


def _photo_check(ctx_pages, row, detail, vins, dry_run, skip_vision=False):
    """The BOL delivery-photo / VIN-on-vehicle step. Tags Delivery confirmed
    (+ No VIN photo if a VIN isn't found on the car) + CLAUDE. Shared by the
    normal clean-pass path and --photos-only."""
    sd_page, tesla_page, claims_page, bol_page = ctx_pages
    tags = [config.TAG_DELIVERY_CONFIRMED]
    if skip_vision:
        print("  decision: clean (vision skipped) -> Delivery confirmed")
    else:
        sections, bol_zip = sd.get_bol_photos(bol_page, detail.detail_url)

        # ACTUAL delivered ZIP: read from the Super Dispatch footer stamp burned
        # into every delivery-inspection photo ("Delivery Condition: 5/23/2026,
        # Santa Clarita, CA 91355") — the carrier app generates it at the drop
        # location, so it reflects where the photos were really taken. The BOL
        # timeline ZIP is only the fallback when no footer is readable.
        all_urls = [u for s in sections for u in s["urls"]]
        footer_imgs = sd.fetch_images(bol_page, all_urls[:5])
        fz = ocr.footer_zip(footer_imgs)
        delivered_zip = fz or bol_zip
        zsrc = ("photo footer" if fz
                else "BOL timeline (no readable footer)" if bol_zip else "?")

        # ZIP check: scheduled delivery ZIP (order page) vs ACTUAL delivered ZIP.
        # If they differ and Claude judges them too far apart by road, it's a
        # wrong-site delivery -> tag ZIP CODE + CLAUDE and stop.
        sched = (detail.delivery_zip or "").strip()
        deliv = (delivered_zip or "").strip()
        print(f"  ZIP: scheduled={sched or '?'}  delivered={deliv or '?'}  [{zsrc}]")
        if sched and deliv and sched != deliv:
            zc = zipdist.check(sched, deliv, config.ZIP_DRIVE_MINUTES)
            print(f"    zip mismatch -> ~{zc.drive_minutes}min  "
                  f"too_far={zc.too_far}  ({zc.reasoning})")
            if zc.too_far:
                ztags = [config.TAG_ZIP, config.TAG_CLAUDE]
                print(f"  decision: wrong delivery site ({sched} vs {deliv}) -> {ztags}")
                if dry_run:
                    print(f"  -> [dry-run] would flag {ztags}")
                else:
                    apply_tags(sd_page, row, detail.edit_url, ztags)
                return

        total = sum(len(s["urls"]) for s in sections)
        print(f"  BOL: {total} photos across {len(sections)} section(s)")
        missing_photo = False
        mismatch = False
        for idx, vin in enumerate(vins):
            # Each vehicle has its OWN delivery section. Scan only this VIN's
            # section, so we don't send another vehicle's photos for this VIN.
            sec = _section_for_vin(sections, vin, idx, len(vins))
            urls = sec["urls"] if sec else [u for s in sections for u in s["urls"]]
            images = sd.fetch_images(bol_page, urls)
            cand = ocr.scan_for_vin(images, vin)   # lazy OCR, stops at first full-VIN read
            if not cand:
                print(f"    {vin}: no VIN found in its {len(urls)}-photo section "
                      f"-> No VIN photo")
                missing_photo = True
                continue
            sent = [images[i] for i in cand]
            print(f"    {vin}: {len(urls)}-photo section, sending {len(sent)} to API")
            v = vision.analyze_delivery_photos(sent, vin)
            # Decide the match in CODE from what Claude READ, rather than trusting
            # its conservative yes/no (it under-confirms legible-but-glary VINs).
            m = _vin_match(v.vin_read, vin)
            found = v.vin_photo_found or (m is True)
            is_mismatch = v.vin_mismatch or (m is False)
            print(f"      vin_photo={v.vin_photo_found} read={v.vin_read!r} "
                  f"match={m} -> found={found} mismatch={is_mismatch} conf={v.confidence}")
            if TROUBLESHOOT and (not found or is_mismatch
                                 or v.confidence < TROUBLESHOOT_CONF):
                print(f"      reason: {v.reasoning or '(none given)'}")
            if is_mismatch:
                # A legible but WRONG VIN was photographed -> wrong car delivered.
                print(f"      !! {vin}: photographed VIN {v.vin_read!r} does not match")
                mismatch = True
            elif not found:
                missing_photo = True

        # A confirmed wrong-VIN read is the most serious finding and drives the tag.
        if mismatch:
            mtags = [config.TAG_VIN_MISMATCH, config.TAG_CLAUDE]
            print(f"  decision: VIN mismatch -> {mtags}")
            if dry_run:
                print(f"  -> [dry-run] would flag {mtags}")
            else:
                apply_tags(sd_page, row, detail.edit_url, mtags)
            return

        if missing_photo:
            tags.append(config.TAG_NO_VIN_PHOTOS)
        print(f"  decision: {tags}")

    tags.append(config.TAG_CLAUDE)        # CLAUDE on every tagged shipment
    if dry_run:
        print(f"  -> [dry-run] would flag {tags}")
        return
    apply_tags(sd_page, row, detail.edit_url, tags)


def process(ctx_pages, row, dry_run, skip_vision, photos_only=False):
    sd_page, tesla_page, claims_page, bol_page = ctx_pages
    detail = sd.open_order_detail(sd_page, row)
    vins = [v.vin for v in detail.vehicles]
    print(f"\n{row.order_id}  tags={row.tags}  VIN(s)={vins}")
    if not vins:
        print("  no VINs extracted — skipping")
        return

    # --photos-only: skip both Tesla checks (payment + claims) and jump straight
    # to the BOL delivery-photo / VIN-on-vehicle step.
    if photos_only:
        print("  (photos-only: skipping Tesla payment + claims)")
        _photo_check(ctx_pages, row, detail, vins, dry_run)
        return

    blank_payment = False
    has_claim = False
    indeterminate = False
    for vin in vins:
        pay = tesla.payment_check(tesla_page, vin)
        claim = tesla.claims_check(claims_page, vin)
        print(f"  {vin}  payment={pay.ok}/{pay.status!r}  "
              f"claim={claim.has_claim}({claim.record_count})")
        if pay.indeterminate or claim.indeterminate:
            # Portal wouldn't load this VIN even after retries — don't guess.
            indeterminate = True
            print(f"    !! Tesla read failed for {vin} "
                  f"(payment={pay.note or 'ok'}; claim={'err' if claim.indeterminate else 'ok'})")
            continue
        if not pay.ok:
            blank_payment = True
        if claim.has_claim:
            has_claim = True

    if indeterminate:
        # Leave the shipment completely untouched for manual review.
        print("  decision: Tesla portal unreadable -> SKIP (manual review, no tags)")
        return

    if blank_payment:
        # Missing payment -> SUS + CLAUDE only (nothing else). Move on.
        tags = [config.TAG_SUS, config.TAG_CLAUDE]
        print("  decision: payment missing -> SUS")
        if dry_run:
            print(f"  -> [dry-run] would flag {tags}")
        else:
            apply_tags(sd_page, row, detail.edit_url, tags)
        return
    if has_claim:
        tags = [config.TAG_DAMAGE_CLAIM, config.TAG_CLAUDE]
        print("  decision: damage claim")
        if dry_run:
            print(f"  -> [dry-run] would flag {tags}")
        else:
            apply_tags(sd_page, row, detail.edit_url, tags)
        return

    # Clean so far -> BOL / photo check.
    _photo_check(ctx_pages, row, detail, vins, dry_run, skip_vision)


def _process_orders(pages, orders, dry_run, skip_vision, photos_only):
    errors = 0
    for i, row in enumerate(orders, 1):
        print(f"\n[{i}/{len(orders)}]", end="")
        try:
            process(pages, row, dry_run, skip_vision, photos_only)
        except Exception as exc:
            errors += 1
            print(f"  !! ERROR on {row.order_id}: "
                  f"{type(exc).__name__}: {exc}\n  ...skipping, continuing batch")
    if errors:
        print(f"\n{errors}/{len(orders)} shipment(s) errored and were skipped.")


def main(args):
    import runlog
    import time
    print(f"Logging this run to {runlog.start('reconcile')}")

    # OCR engine preflight — fail loud, not silent. If the configured engine isn't
    # usable (e.g. OCR_ENGINE=easyocr but easyocr isn't installed in this venv),
    # every photo's OCR would throw and get swallowed -> every VIN falsely "No VIN
    # photo", instantly. Surface it and fall back to tesseract if that one works.
    ok, msg = ocr.check_engine()
    print(f"OCR engine: {msg}")
    if not ok:
        ocr.OCR_ENGINE = "tesseract"
        tv = ocr.tesseract_ok()
        if tv:
            print(f"  -> falling back to tesseract {tv} for this run.")
        else:
            print("  -> tesseract is ALSO unavailable; VIN photo checks will all "
                  "report 'No VIN photo'. Install an OCR engine before trusting "
                  "this run.")

    with browser_context() as ctx:
        sd_page = ctx.new_page()
        tesla_page = ctx.new_page()
        claims_page = ctx.new_page()
        bol_page = ctx.new_page()
        pages = (sd_page, tesla_page, claims_page, bol_page)

        # ---- single-order mode: one shot, ignores window/skip/loop ----
        if args.order:
            print(f"Single-order test: {args.order}  dry_run={args.dry_run}  "
                  f"skip_vision={args.skip_vision}")
            print("=" * 60)
            row = sd.find_order_by_id(sd_page, args.order)
            if not row:
                print(f"Order {args.order!r} not found in SuperDispatch.")
                return
            try:
                row.tags = sd.read_edit_tags(sd_page, sd.edit_url_for(row.detail_url))
            except Exception:
                row.tags = []
            print(f"Resolved {args.order} -> {row.detail_url}  existing tags={row.tags}")
            if not args.photos_only:
                tesla.setup_claims_filters(claims_page)
                tesla.ensure_approved(tesla_page)
            _process_orders(pages, [row], args.dry_run, args.skip_vision, args.photos_only)
            input("\nPress Enter to close the browser...")
            return

        # ---- window mode: one pass (default), or --loop forever ----
        if not args.photos_only:
            tesla.setup_claims_filters(claims_page)     # once per session
            tesla.ensure_approved(tesla_page)

        pass_no = 0
        while True:
            pass_no += 1
            limit = "all" if not args.count else args.count
            print(f"\n===== pass {pass_no}  {dt.datetime.now():%Y-%m-%d %H:%M} =====")
            print(f"Window {WINDOW_START}..{WINDOW_END}  count={limit}  "
                  f"dry_run={args.dry_run}  skip_vision={args.skip_vision}")
            orders = collect_qualifying(sd_page, args.count, args.max_pages)
            if not orders:
                print("No qualifying shipments found.")
            else:
                print(f"Found {len(orders)} qualifying shipment(s).")
                _process_orders(pages, orders, args.dry_run, args.skip_vision, args.photos_only)

            if not args.loop:
                break
            print(f"\nPass {pass_no} complete. Sleeping {args.interval} min until the "
                  f"next pass (Ctrl+C to stop)...")
            time.sleep(args.interval * 60)

        print("\n" + "=" * 60)
        # Default run auto-closes the browser and quits when the task finishes (and
        # on a timeout/error, the `with browser_context()` block closes it too).
        # Only pause for a manual close in the interactive modes; --order takes the
        # single-order path above, which always pauses.
        if args.headed or args.troubleshoot:
            input("Press Enter to close the browser...")
        else:
            print("Done — closing the browser and exiting.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=0,
                    help="Max qualifying shipments to process; 0 = all (default).")
    ap.add_argument("--max-pages", type=int, default=80,
                    help="Invoiced-list pages to scan (20 orders each). The window "
                         "can hold ~80 pages, so 80 reaches the recent deliveries.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-vision", action="store_true")
    ap.add_argument("--loop", action="store_true",
                    help="Keep running: re-scan every --interval minutes until stopped (Ctrl+C).")
    ap.add_argument("--interval", type=int, default=30,
                    help="Minutes between passes in --loop mode (default 30).")
    ap.add_argument("--order", help="Process ONE shipment by its SuperDispatch "
                    "order ID (e.g. A1YE586). Ignores the date window and the "
                    "skip filter; pair with --dry-run to avoid writing tags.")
    ap.add_argument("--photos-only", action="store_true",
                    help="Skip both Tesla checks (payment + claims) and run only "
                         "the BOL delivery-photo / VIN-on-vehicle step.")
    ap.add_argument("--headed", action="store_true",
                    help="Force a visible, interactive browser window for this run "
                         "(overrides WINDOW_MODE=ghost / HEADLESS).")
    ap.add_argument("--troubleshoot", action="store_true",
                    help="Print Claude's short reasoning whenever a VIN doesn't cleanly "
                         "pass (not found / mismatch / low confidence), to see WHY.")
    args = ap.parse_args()
    if args.headed:
        config.WINDOW_MODE = "visible"
        config.HEADLESS = False
    TROUBLESHOOT = args.troubleshoot
    main(args)
