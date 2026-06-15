"""
The ONLY place the model is used: reading the VIN off the vehicle in the
delivery photos and checking the location stamp. Everything else is plain code.

Photos are downscaled before sending (cost control). All inspection photos may
be passed in; the model is told to use ONLY the ones stamped "Delivery
Condition", so pickup photos are ignored regardless of how they were collected.
"""
from __future__ import annotations
import base64
import json
from io import BytesIO
from typing import Iterable

import anthropic
from PIL import Image

import config
from models import VisionResult, ZipCheckResult

_client: anthropic.Anthropic | None = None

# Send the WHOLE frame (Claude needs context to judge WHERE the VIN is — windshield
# vs. fob vs. paperwork). 1568px / q90 is Claude's high-fidelity sweet spot: enough
# resolution to read a faint etched windshield VIN without wasting tokens.
_MAX_SIDE = 1568          # downscale longest edge to this many px
_JPEG_QUALITY = 90

_TOOL = {
    "name": "report",
    "description": "Report what the pre-selected delivery photos show.",
    "input_schema": {
        "type": "object",
        "properties": {
            "vin_photo_found": {
                "type": "boolean",
                "description": "True ONLY if a photo shows a VIN matching the expected "
                               "VIN AND that VIN is a permanent part of THIS vehicle: the "
                               "windshield VIN plate, a door-jamb VIN sticker, the car's "
                               "own center display/touchscreen, or a windshield QR-code "
                               "sticker with the VIN written on it. False if the VIN "
                               "appears on anything detached from or merely near the car "
                               "(key/fob tag, loose printout, paperwork/BOL, a phone or "
                               "non-dash screen, a handwritten note, or any label not "
                               "affixed to the vehicle), or if no matching VIN is legible.",
            },
            "vin_read": {"type": "string",
                         "description": "The VIN as read off the vehicle, or empty if none legible."},
            "vin_mismatch": {
                "type": "boolean",
                "description": "True ONLY if you can clearly read a complete or "
                               "near-complete VIN on/in the photos that is legibly "
                               "DIFFERENT from the Expected VIN (i.e. the wrong car's "
                               "VIN). Must be a confident, legible read — NOT a blurry "
                               "or partial read that merely differs by a character. "
                               "False if the VIN you read matches the Expected VIN, or "
                               "if you cannot clearly read any VIN.",
            },
            "confidence": {"type": "number",
                           "description": "0..1 confidence that the VIN is correct AND attached to the car."},
            "reasoning": {"type": "string",
                          "description": "ONE short sentence explaining this decision — "
                                         "especially WHY vin_photo_found is false or "
                                         "confidence is low: where the VIN appeared (which "
                                         "photo / what surface), what was illegible, glare, "
                                         "wrong-surface, partial read, etc. Always fill this."},
            "notes": {"type": "string", "description": "Any caveat worth a human review."},
        },
        "required": ["vin_photo_found", "vin_read", "vin_mismatch", "confidence", "reasoning"],
    },
}

_PROMPT = (
    "You are auditing a small set of vehicle delivery photos that were pre-selected "
    "because they likely show this vehicle's VIN. Your job is NOT to find a VIN among "
    "many photos — it is to judge two things rigorously.\n"
    "Expected VIN: {vin}.\n"
    "1) IS THIS ACTUALLY THE RIGHT VIN? Read every VIN-like string you can see and "
    "compare it, character by character, to the Expected VIN — pay special attention to "
    "the last 6 digits. Treat it as a match only if the legible characters agree with "
    "the Expected VIN; a different VIN, or a number that merely looks similar, is NOT a "
    "match. Report what you read in vin_read. If you can clearly and confidently read a "
    "full (or nearly full) VIN that is legibly DIFFERENT from the Expected VIN — i.e. the "
    "wrong car's VIN — set vin_mismatch = true. Do NOT set vin_mismatch for a blurry or "
    "partial read that just differs by a character; only when you are sure it is a "
    "different VIN.\n"
    "2) IS THE VIN PHYSICALLY ATTACHED TO THIS VEHICLE? This is the most important "
    "judgment. The VIN counts only if it appears as a permanent part of the car itself, "
    "in one of these places:\n"
    "   - the windshield VIN plate (etched/printed at the base of the windshield)\n"
    "   - a door-jamb VIN sticker affixed to the car\n"
    "   - the vehicle's own center display / touchscreen showing the VIN\n"
    "   - a QR-code sticker on the windshield with the VIN written on it\n"
    "It does NOT count if the VIN appears on anything detached from or merely placed near "
    "the car: a key or key-fob tag, a loose printout, paperwork or a BOL, a phone or other "
    "screen that isn't the car's dash, a handwritten note, or a sticker/label not affixed "
    "to the vehicle. When unsure whether the surface is part of the car, lower your "
    "confidence and explain in notes.\n"
    "Legibility: a windshield VIN strip (etched, low-contrast, often beside the Tesla 'T' "
    "and a small barcode) or a door-jamb sticker is frequently faint or partly washed out "
    "by sun glare and reflections. You do NOT need all 17 characters crisp — if the plate "
    "is clearly mounted on the car and the characters you CAN read are consistent with the "
    "Expected VIN, set vin_photo_found = true. Reserve false for when the VIN genuinely "
    "can't be made out, or it's on a non-vehicle surface, or it's clearly a different VIN.\n"
    "Stay strict about WHERE the VIN is (it must be on the car), but don't withhold a true "
    "just because glare softened a few characters.\n"
    "Always fill `reasoning` with one short sentence on why you decided as you did — "
    "above all when vin_photo_found is false or confidence is low, say where the VIN was "
    "and what stopped it from passing. Call the `report` tool with your findings."
)


def _get_client() -> anthropic.Anthropic:
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in .env.")
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def _downscale(raw: bytes) -> bytes:
    try:
        im = Image.open(BytesIO(raw)).convert("RGB")
        im.thumbnail((_MAX_SIDE, _MAX_SIDE))
        out = BytesIO()
        im.save(out, format="JPEG", quality=_JPEG_QUALITY)
        return out.getvalue()
    except Exception:
        return raw


def analyze_delivery_photos(
    images: Iterable[bytes],
    expected_vin: str,
) -> VisionResult:
    blocks: list[dict] = [{
        "type": "text",
        "text": _PROMPT.format(vin=expected_vin),
    }]
    for raw in images:
        small = _downscale(raw)
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(small).decode(),
            },
        })

    resp = _get_client().messages.create(
        model=config.VISION_MODEL,
        max_tokens=500,
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "report"},
        messages=[{"role": "user", "content": blocks}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "report":
            d = block.input
            return VisionResult(
                vin_photo_found=bool(d.get("vin_photo_found")),
                vin_read=d.get("vin_read") or None,
                vin_mismatch=bool(d.get("vin_mismatch")),
                confidence=float(d.get("confidence", 0.0)),
                reasoning=(d.get("reasoning") or d.get("notes") or "").strip(),
                raw=json.dumps(d),
            )
    return VisionResult(vin_photo_found=False, raw="no tool_use in response")


# ----------------------- ZIP distance (text-only) -----------------------
_ZIP_TOOL = {
    "name": "zipcompare",
    "description": "Report the driving relationship between two US ZIP codes.",
    "input_schema": {
        "type": "object",
        "properties": {
            "drive_minutes": {
                "type": "number",
                "description": "Best estimate of typical driving time IN MINUTES "
                               "between the centers of the two ZIP codes (0 if same ZIP).",
            },
            "same_metro": {
                "type": "boolean",
                "description": "True if the two ZIPs are in the same city/metro area.",
            },
            "too_far": {
                "type": "boolean",
                "description": "True if the driving time is at least the given "
                               "threshold of minutes apart (i.e. likely a different "
                               "delivery site).",
            },
            "reasoning": {"type": "string",
                          "description": "One short sentence on the locations and distance."},
        },
        "required": ["drive_minutes", "too_far"],
    },
}

_ZIP_PROMPT = (
    "You are auditing a vehicle delivery. A car was SCHEDULED to be delivered to one "
    "US ZIP code but the carrier's records show it was actually delivered to another. "
    "Scheduled delivery ZIP: {sched}. Actual delivered ZIP: {actual}.\n"
    "Using your knowledge of US geography, estimate the typical DRIVING TIME in minutes "
    "between the centers of these two ZIP codes. If they are the same ZIP, that's 0. "
    "Set too_far = true if that driving time is {threshold} minutes or more (which would "
    "mean the car was likely dropped at the wrong site), and false if they're within "
    "{threshold} minutes (same or neighboring area). If you are uncertain, lean toward "
    "flagging (too_far = true) and say so. Call the `zipcompare` tool."
)


def zip_drive_check(scheduled_zip: str, delivered_zip: str,
                    threshold_min: int = 20) -> ZipCheckResult:
    """Ask Claude to estimate whether two ZIPs are >= threshold driving minutes
    apart. Only call this when the ZIPs differ. Never raises — on any error it
    returns too_far=False with a note, so a model hiccup won't mis-flag an order."""
    try:
        resp = _get_client().messages.create(
            model=config.VISION_MODEL,
            max_tokens=300,
            tools=[_ZIP_TOOL],
            tool_choice={"type": "tool", "name": "zipcompare"},
            messages=[{"role": "user", "content": _ZIP_PROMPT.format(
                sched=scheduled_zip, actual=delivered_zip, threshold=threshold_min)}],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "zipcompare":
                d = block.input
                mins = d.get("drive_minutes")
                return ZipCheckResult(
                    too_far=bool(d.get("too_far")),
                    drive_minutes=int(mins) if isinstance(mins, (int, float)) else None,
                    same_metro=d.get("same_metro"),
                    reasoning=d.get("reasoning") or "",
                    raw=json.dumps(d),
                )
        return ZipCheckResult(too_far=False, reasoning="no tool_use in response")
    except Exception as exc:
        return ZipCheckResult(too_far=False, reasoning=f"zip check error: {exc}")
