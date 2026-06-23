"""
Worker — pulls items off the durable queue and does the slow work.

Runs as its own long-running process (systemd), separate from the listener, so the
listener never blocks.

TWO-TIER EVENT ROUTING (the timing rule — easy to get wrong):
  * order.picked_up / order.manually_marked_as_picked_up  (STATUS events)
        -> the order just flipped to picked up. Photos are NOT ready yet.
        -> fetch order details via the API, upsert the shipment (+ VINs), UI push.
  * order.picked_up_bol                                   (BOL/PHOTO event)
        -> fires only once the pickup BOL/photos exist.
        -> resolve the order's number via the API, then drive the Super Dispatch
           WEB app (Playwright) to: open the order, download the PICKUP inspection
           photos, easyOCR each shipment VIN, and apply the VIN/NO VIN + CLAUDE tags
           on the edit page. (Web app because the API can't write tags — for now.)
  * order.picked_up.ignored                               (optional) -> just note it.

IDEMPOTENCY: queue keyed on event guid; photos skip re-download by content hash;
an order skips re-tagging once tagged (db.order_tagged); shipment writes upsert.

Run (dev):  python worker.py
Prod:       systemd (see systemd/direct-pickup-worker.service)
"""
from __future__ import annotations
import hashlib
import json
import os
import signal
import time
from typing import Optional

import browser
import config
import db
import sd_client
import sd_web
import tagging
from logging_setup import setup, get_logger

setup("worker")
log = get_logger(__name__)

_stop = False


def _handle_signal(signum, _frame):
    global _stop
    _stop = True
    log.info("signal received, finishing current item then exiting", extra={"signal": signum})


# --- get-order field readers (VERIFY names against the live reference) ------
def _order_number(order: dict) -> Optional[str]:
    return order.get("number") or order.get("order_number")


def _order_status(order: dict) -> Optional[str]:
    return order.get("status") or (order.get("status_info") or {}).get("status")


def _picked_up_at(order: dict) -> Optional[str]:
    return (order.get("picked_up_at") or order.get("pickup_completed_at")
            or ((order.get("pickup") or {}).get("completed_at")))


def _vins(order: dict) -> list[str]:
    return [v.get("vin") for v in (order.get("vehicles") or []) if v.get("vin")]


def _photo_path(order_guid: str, vin: Optional[str], url: str) -> tuple[str, str]:
    """photos/<order_guid>/<vin or _>/<url-hash><ext>. Returns (path, photo_id)."""
    pid = hashlib.sha1(url.encode()).hexdigest()[:16]
    ext = os.path.splitext(url.split("?")[0])[1] or ".jpg"
    folder = os.path.join(config.PHOTO_DIR, order_guid, vin or "_")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{pid}{ext}"), pid


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------
def handle_status_event(order_guid: str, action: str) -> None:
    """order.picked_up / ..manually.. -> record the shipment + UI push (API only)."""
    order = sd_client.get_order(order_guid)
    vins = _vins(order)
    db.upsert_shipment(
        order_guid=order_guid, number=_order_number(order),
        status=_order_status(order) or "picked_up",
        picked_up_at=_picked_up_at(order), details=order, vins=vins)
    db.push_ui_event(order_guid=order_guid, kind="picked_up",
                     payload={"action": action, "number": _order_number(order), "vins": vins})
    log.info("shipment recorded", extra={"order_guid": order_guid,
                                         "number": _order_number(order),
                                         "vins": len(vins), "action": action})


def handle_bol_event(order_guid: str) -> None:
    """order.picked_up_bol -> open in web UI, download pickup photos, OCR, tag."""
    if db.order_tagged(order_guid):
        log.info("already tagged — skipping", extra={"order_guid": order_guid})
        return

    # API: authoritative order number + VIN list (the web view uuid != API guid).
    order = sd_client.get_order(order_guid)
    number = _order_number(order)
    expected_vins = _vins(order)
    if not number:
        raise RuntimeError(f"no order number for {order_guid} — can't locate it in the web UI")

    # Only tag orders whose number/name carries a required marker (e.g. "-direct" /
    # "trade"). Others are left untagged (no browser work). Blank markers -> tag all.
    markers = config.TAG_NAME_MARKERS
    if markers and not any(m in number.lower() for m in markers):
        log.info("order name lacks required marker — not tagging",
                 extra={"order_guid": order_guid, "number": number, "markers": list(markers)})
        db.push_ui_event(order_guid=order_guid, kind="tag_skipped",
                         payload={"number": number, "reason": "name_marker", "markers": list(markers)})
        return

    log.info("BOL event: locating order in web UI", extra={"order_guid": order_guid,
                                                           "number": number,
                                                           "vins": len(expected_vins)})

    # Web app: open, download pickup photos, decide tags, apply tags.
    with browser.browser_context() as ctx:
        page = ctx.new_page()
        detail_url = sd_web.find_order_detail_url(page, number)
        if not detail_url:
            raise RuntimeError(f"order {number} not found in the Super Dispatch web UI")

        sections = sd_web.get_pickup_photos(page, detail_url)
        groups, downloaded = [], 0
        for sec in sections:
            sec_vin = sec.get("vin")
            images = sd_web.fetch_images(page, sec.get("urls") or [])
            for url, data in zip(sec.get("urls") or [], images):
                path, pid = _photo_path(order_guid, sec_vin, url)
                if not db.photo_exists(pid):
                    with open(path, "wb") as fh:
                        fh.write(data)
                    db.record_photo(photo_id=pid, order_guid=order_guid, vin=sec_vin,
                                    step="pickup", subject=None, taken_at=None,
                                    latitude=None, longitude=None, source_url=url,
                                    local_path=path)
                    downloaded += 1
            groups.append({"vin": sec_vin, "images": images})
        log.info("pickup photos downloaded", extra={"order_guid": order_guid,
                                                    "sections": len(sections),
                                                    "downloaded": downloaded})

        # Decide (easyOCR) + apply the tags on the order's edit page.
        decision = tagging.decide_order_tags(groups, expected_vins)
        sd_web.add_tags(page, sd_web.edit_url_for(detail_url), decision["tags"])

    db.record_order_tagging(order_guid=order_guid, vin_result=decision["vin_result"],
                            applied_tags=decision["tags"], detail=decision)
    db.push_ui_event(order_guid=order_guid, kind="photos_tagged",
                     payload={"number": number, "vin_result": decision["vin_result"],
                              "tags": decision["tags"], "per_vin": decision["per_vin"],
                              "photos_seen": decision["photos_seen"]})
    log.info("order tagged", extra={"order_guid": order_guid, "number": number,
                                    "tags": decision["tags"],
                                    "vin_result": decision["vin_result"]})


def process(item) -> None:
    """Route one queue item to the right handler by its action."""
    action = item["action"]
    order_guid = item["order_guid"]
    if not order_guid:
        payload = json.loads(item["payload"] or "{}")
        order_guid = payload.get("order_guid") or payload.get("object_guid")
    if not order_guid:
        raise RuntimeError(f"no order_guid for event {item['event_guid']} ({action})")

    if action in config.PICKUP_STATUS_ACTIONS:
        handle_status_event(order_guid, action)
        # Manually-marked pickups won't get a later BOL/photo event, so check photos
        # and tag NOW (no photos -> NO VIN). Driver-app pickups still tag on their
        # own order.picked_up_bol event when the photos finish uploading.
        if action == config.PICKUP_MANUAL_ACTION:
            handle_bol_event(order_guid)
    elif action == config.PICKUP_BOL_ACTION:
        handle_bol_event(order_guid)
    elif action == config.PICKUP_IGNORED_ACTION:
        log.info("pickup ignored event", extra={"order_guid": order_guid})
        db.push_ui_event(order_guid=order_guid, kind="picked_up_ignored", payload={})
    else:
        log.warning("unhandled action", extra={"action": action, "order_guid": order_guid})


def run_once() -> bool:
    """Claim and process one item. Returns True if it did work, False if idle."""
    item = db.claim_next()
    if item is None:
        return False
    try:
        process(item)
        db.mark_done(item["id"])
        log.info("done", extra={"id": item["id"], "action": item["action"]})
    except Exception as exc:                       # noqa: BLE001 — keep the worker alive
        status = db.mark_failed(item["id"], repr(exc), max_attempts=config.WORKER_MAX_ATTEMPTS)
        log.exception("item failed", extra={"id": item["id"], "action": item["action"],
                                            "new_status": status})
    return True


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    db.init_db()
    log.info("worker up", extra={"poll_s": config.WORKER_POLL_SECONDS,
                                 "max_attempts": config.WORKER_MAX_ATTEMPTS})
    while not _stop:
        try:
            did_work = run_once()
        except Exception:                          # noqa: BLE001 — never die on a claim error
            log.exception("run_once crashed")
            did_work = False
        if not did_work:
            time.sleep(config.WORKER_POLL_SECONDS)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
