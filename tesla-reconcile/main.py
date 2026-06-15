"""
Orchestrator.

Run:  python main.py --max-orders 5        # test on a few
      python main.py                        # full window
      python main.py --dry-run              # decide but do NOT apply tags
      python main.py --order ORDER123       # process ONE order by id (any age)

Flow per order:
  scrape -> skip? -> open detail -> for each VIN: payment + claims (pure code)
         -> if any claim: tag Damage claim
         -> else: vision on delivery photos -> Delivery confirmed (+ No VIN photos)
  Any unexpected state is screenshotted and pushed to the review queue; the run
  continues.
"""
from __future__ import annotations
import argparse
import csv
import json
import os
import traceback
from datetime import date

from playwright.sync_api import Page

import config
import superdispatch as sd
import tesla
import vision
from auth import browser_context
from models import OrderDetail, OrderOutcome


def decide_order(page: Page, claims_page: Page, detail: OrderDetail,
                 skip_vision: bool = False) -> OrderOutcome:
    """Run the checks for one order and return the tag decision (no writes here)."""
    has_claim = False
    for veh in detail.vehicles:
        pay = tesla.payment_check(page, veh.vin)
        if not pay.ok:
            return OrderOutcome(
                order_id=detail.order_id,
                decision=f"SKIPPED: payment blank ({veh.vin})",
                needs_review=False,
                detail=pay.note,
            )
        claim = tesla.claims_check(claims_page, veh.vin)
        if claim.has_claim:
            has_claim = True
            break

    if has_claim:
        return OrderOutcome(detail.order_id, decision="Damage claim",
                            tags_applied=[config.TAG_DAMAGE_CLAIM])

    if skip_vision:
        # Validate everything except the photo step: assume the clean-pass tag.
        return OrderOutcome(detail.order_id, decision="Delivery confirmed (vision skipped)",
                            tags_applied=[config.TAG_DELIVERY_CONFIRMED],
                            detail="vision skipped")

    # Visual checks on the delivery photos.
    urls = sd.get_bol_delivery_photo_urls(page, detail.detail_url)
    images = _fetch_images(page, urls)
    expected_vin = detail.vehicles[0].vin if detail.vehicles else ""
    vres = vision.analyze_delivery_photos(images, expected_vin)

    tags = [config.TAG_DELIVERY_CONFIRMED]
    detail_note = vres.raw
    if not vres.vin_photo_found:
        tags.append(config.TAG_NO_VIN_PHOTOS)

    return OrderOutcome(detail.order_id, decision="+".join(tags),
                        tags_applied=tags, needs_review=False, detail=detail_note)


def run_single(order_id: str, dry_run: bool, skip_vision: bool = False):
    """Process ONE order by id, ignoring the delivery-window/skip-tag logic.

    Looks the order up by its id in the SuperDispatch search (which first switches
    the time-frame dropdown to "All time" — see sd.select_all_time — so OLD orders
    are found, not just recent ones), then runs the same payment/claims/vision
    decision and tagging as a normal run.
    """
    _ensure_dirs()
    print(f"Single order: {order_id}  (dry_run={dry_run})")
    with browser_context() as ctx:
        page = ctx.new_page()
        claims_page = ctx.new_page()
        tesla.setup_claims_filters(claims_page)

        row = sd.find_order_by_id(page, order_id)
        if not row:
            print(f"Order {order_id!r} not found in SuperDispatch "
                  f"(is the time frame set to All time?).")
            return
        try:
            detail = sd.open_order_detail(page, row)
            outcome = decide_order(page, claims_page, detail, skip_vision)
            if not dry_run and outcome.tags_applied:
                sd.add_tags(page, detail.edit_url, outcome.tags_applied)
            _log(outcome, dry_run)
        except Exception as exc:
            _escalate(page, row.order_id, exc)


def run(max_orders: int | None, dry_run: bool, skip_vision: bool = False):
    _ensure_dirs()
    start, end = sd.default_window()
    print(f"Window: {start} .. {end}  (dry_run={dry_run})")

    processed = 0
    with browser_context() as ctx:
        page = ctx.new_page()
        claims_page = ctx.new_page()          # dedicated tab for the Claims filters
        tesla.setup_claims_filters(claims_page)   # one-time per session

        page_num = 1
        while True:
            page.goto(sd.invoiced_url(start, end, page=page_num, ascending=True))
            rows = sd.scrape_order_rows(page)
            if not rows:
                break

            for row in rows:
                if max_orders and processed >= max_orders:
                    return
                if row.should_skip:
                    _log(OrderOutcome(row.order_id, decision="skip (tag)"), dry_run)
                    continue
                try:
                    detail = sd.open_order_detail(page, row)
                    outcome = decide_order(page, claims_page, detail, skip_vision)
                    if not dry_run and outcome.tags_applied:
                        sd.add_tags(page, detail.edit_url, outcome.tags_applied)
                    _log(outcome, dry_run)
                except Exception as exc:                      # edge case -> review
                    _escalate(page, row.order_id, exc)
                finally:
                    processed += 1

            page_num += 1


# ----------------------- io helpers -----------------------
def _fetch_images(page: Page, urls: list[str]) -> list[tuple[bytes, str]]:
    out = []
    for u in urls:
        resp = page.context.request.get(u)
        if resp.ok:
            ct = resp.headers.get("content-type", "image/jpeg").split(";")[0]
            out.append((resp.body(), ct))
    return out


def _log(outcome: OrderOutcome, dry_run: bool):
    print(f"[{outcome.order_id}] {outcome.decision}"
          f"{'  (DRY)' if dry_run else ''}"
          f"{'  ** REVIEW **' if outcome.needs_review else ''}")
    new = not os.path.exists(config.LOG_CSV)
    with open(config.LOG_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["order_id", "decision", "tags", "needs_review", "detail"])
        w.writerow([outcome.order_id, outcome.decision,
                    "|".join(outcome.tags_applied), outcome.needs_review,
                    outcome.detail])
    if outcome.needs_review:
        _queue(outcome.order_id, outcome.detail)


def _escalate(page: Page, order_id: str, exc: Exception):
    shot = os.path.join(config.SCREENSHOT_DIR, f"{order_id}.png")
    try:
        page.screenshot(path=shot)
    except Exception:
        shot = ""
    print(f"[{order_id}] ERROR -> review queue: {exc}")
    _queue(order_id, f"{exc}\n{traceback.format_exc()}", screenshot=shot)


def _queue(order_id: str, detail: str, screenshot: str = ""):
    with open(config.REVIEW_QUEUE, "a") as f:
        f.write(json.dumps({"order_id": order_id, "detail": detail,
                            "screenshot": screenshot}) + "\n")


def _ensure_dirs():
    for d in ("./output", config.SCREENSHOT_DIR):
        os.makedirs(d, exist_ok=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--order", default=None,
                    help="Process a SINGLE order by its id (searches All time so old "
                         "orders are found), ignoring the delivery window.")
    ap.add_argument("--max-orders", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-vision", action="store_true",
                    help="skip the BOL/photo vision step (validate checks + tagging only)")
    args = ap.parse_args()
    if args.order:
        run_single(args.order, args.dry_run, args.skip_vision)
    else:
        run(args.max_orders, args.dry_run, args.skip_vision)
