"""
The photo check / tagging decision.

Workflow (per shipment / order):
  * Read the VIN(s) off the PICKUP inspection photos with easyOCR.
  * If EVERY VIN in the shipment is found in the photos  -> tags = [VIN]
  * If any VIN is missing / unreadable / not matched     -> tags = [NO VIN]
  (A bot-marker tag is appended only if config.TAG_BOT is set; it's blank here.)

The actual OCR + VIN matching is reused from ocr.py (copied from
tesla-reconcile/ocr.py — easyOCR scene-text reader with the smart rotation
fallback). `ocr.scan_for_vin(images, vin)` returns the indices of photos that
contain that VIN; a non-empty result means "found".

ocr.py lazy-loads easyOCR/torch only when it actually runs, so importing this
module is cheap and tests can mock `ocr.scan_for_vin`.
"""
from __future__ import annotations
from typing import Optional

import config
from logging_setup import get_logger

log = get_logger(__name__)


def _images_for_vin(vin: str, groups: list[dict], pool: list[bytes]) -> list[bytes]:
    """Prefer the photos from THIS VIN's own Pickup Inspection section (matched on
    the section's VIN heading); fall back to the whole photo pool if the section
    isn't identifiable. ocr.scan_for_vin is strict (long consecutive run), so the
    pool fallback won't cross-match a sibling VIN in practice."""
    v = vin.strip().upper()
    for g in groups:
        if (g.get("vin") or "").strip().upper() == v and g.get("images"):
            return g["images"]
    return pool


def decide_order_tags(groups: list[dict], expected_vins: list[str]) -> dict:
    """Decide the tags for one order.

    `groups`: [{"vin": <section heading VIN or None>, "images": [bytes]}] — the
    downloaded pickup photos grouped per vehicle.
    `expected_vins`: the VINs on the order (authoritative, from the API).

    Returns a dict: {vin_result, tags, all_found, per_vin, photos_seen, expected}.
    """
    import ocr  # lazy: only loads easyOCR/torch when we actually decide

    pool = [img for g in groups for img in (g.get("images") or [])]
    expected = [v.strip().upper() for v in expected_vins if v and v.strip()]

    per_vin: dict[str, bool] = {}
    for vin in expected:
        imgs = _images_for_vin(vin, groups, pool)
        try:
            found = bool(ocr.scan_for_vin(imgs, vin))
        except Exception as exc:                       # noqa: BLE001 — never crash tagging
            log.exception("OCR failed for VIN", extra={"vin": vin, "err": repr(exc)})
            found = False
        per_vin[vin] = found

    # No VINs known, or any VIN not found -> NO VIN. All found -> VIN.
    all_found = bool(expected) and all(per_vin.values())
    vin_result = config.TAG_VIN if all_found else config.TAG_NO_VIN
    # Only the VIN/NO VIN tag is applied. The bot marker tag is added only when
    # config.TAG_BOT is set; blank -> omitted, so no "CLAUDE" tag on the order.
    tags = [vin_result] + ([config.TAG_BOT] if config.TAG_BOT else [])

    result = {
        "vin_result": vin_result,
        "tags": tags,
        "all_found": all_found,
        "per_vin": per_vin,
        "photos_seen": len(pool),
        "expected": expected,
    }
    log.info("tag decision", extra={k: result[k] for k in
                                    ("vin_result", "all_found", "per_vin", "photos_seen")})
    return result
