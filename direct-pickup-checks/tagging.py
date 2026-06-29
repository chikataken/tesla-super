"""
The photo check / tagging decision.

Workflow (per shipment / order):
  * Confirm each VIN is present in the PICKUP inspection photos.
  * If EVERY VIN in the shipment is found in the photos  -> tags = [VIN]
  * If any VIN is missing / unreadable / not matched     -> tags = [NO VIN]
  (A bot-marker tag is appended only if config.TAG_BOT is set.)

Two interchangeable VIN-check engines (config.VIN_CHECK_ENGINE):
  * "claude" (default) -> a cheap on-device OCR pass (ocr.scan_for_vin) picks the
    candidate photos that PLAUSIBLY show the VIN from the whole pool, then
    vision.vin_present_in_photos(candidates, vin) asks Claude whether the VIN is
    legibly present, on ANY surface (key fob, paperwork, sticker, dash, windshield —
    placement does NOT matter). Only the candidates are sent to Claude (cost control),
    not the whole set. Unlike tesla-reconcile, it does NOT require the VIN on the car.
  * "ocr"             -> on-device ocr.scan_for_vin(images, vin) only (easyOCR/
    Tesseract, no API calls). Non-empty match list = found. Searches the VIN's own
    section first (strict, to avoid sibling cross-match).

Both engines are imported lazily so this module is cheap to import and tests can
mock `vision.vin_present_in_photos` / `ocr.scan_for_vin`.
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


def _found_via_claude(pool: list[bytes], vin: str) -> bool:
    """Is `vin` legible ANYWHERE in the photos? (surface-agnostic).

    Cost control: only the photos that PLAUSIBLY show this VIN are sent to Claude,
    not the whole set. A cheap on-device OCR pass (ocr.scan_for_vin — rotation-aware,
    ranked, surface-agnostic, designed for exactly 'which photos to send to the API')
    picks the candidate photos from the WHOLE pool; Claude then confirms the exact,
    character-accurate match on just those. No candidates -> not found, no API call.

    Trade-off: a VIN so faint the OCR pre-filter can't catch it at any rotation never
    reaches Claude (lower OCR_MIN_RUN to widen the net, or use a whole-set strategy)."""
    import ocr     # lazy: loads easyOCR/torch only when the claude engine runs
    import vision  # lazy: imports anthropic only when the claude engine runs
    idxs = ocr.scan_for_vin(pool, vin)
    candidates = [pool[i] for i in idxs]
    if not candidates:
        log.info("claude vin check: OCR pre-filter found no candidate photos -> not found",
                 extra={"vin": vin, "photos": len(pool)})
        return False
    res = vision.vin_present_in_photos(candidates, vin)
    log.info("claude vin check", extra={"vin": vin, "candidates": len(candidates),
                                        "present": res.vin_present,
                                        "vin_read": res.vin_read,
                                        "confidence": res.confidence,
                                        "reasoning": res.reasoning})
    return res.vin_present


def _found_via_ocr(imgs: list[bytes], vin: str) -> bool:
    """Is `vin` present in `imgs` per the on-device reader? (non-empty match list)."""
    import ocr  # lazy: only loads easyOCR/torch when we actually decide
    return bool(ocr.scan_for_vin(imgs, vin))


def decide_order_tags(groups: list[dict], expected_vins: list[str],
                      engine: Optional[str] = None) -> dict:
    """Decide the tags for one order.

    `groups`: [{"vin": <section heading VIN or None>, "images": [bytes]}] — the
    downloaded pickup photos grouped per vehicle.
    `expected_vins`: the VINs on the order (authoritative, from the API).
    `engine`: "claude" or "ocr"; defaults to config.VIN_CHECK_ENGINE.

    Returns a dict: {vin_result, tags, all_found, per_vin, photos_seen, expected, engine}.
    """
    engine = (engine or config.VIN_CHECK_ENGINE).strip().lower()
    pool = [img for g in groups for img in (g.get("images") or [])]
    expected = [v.strip().upper() for v in expected_vins if v and v.strip()]

    per_vin: dict[str, bool] = {}
    for vin in expected:
        try:
            if engine == "ocr":
                # OCR is strict, so prefer the VIN's own section then fall back to pool.
                found = _found_via_ocr(_images_for_vin(vin, groups, pool), vin)
            else:
                # Claude: OCR pre-filters the WHOLE pool to candidate photos (the VIN
                # may be on any photo/surface), then Claude confirms only those.
                found = _found_via_claude(pool, vin)
        except Exception as exc:                       # noqa: BLE001 — never crash tagging
            log.exception("VIN check failed", extra={"vin": vin, "engine": engine,
                                                     "err": repr(exc)})
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
        "engine": engine,
    }
    log.info("tag decision", extra={k: result[k] for k in
                                    ("vin_result", "all_found", "per_vin", "photos_seen", "engine")})
    return result
