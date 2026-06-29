"""
Claude vision VIN check — confirms the expected VIN is LEGIBLE ANYWHERE in the
pickup photo set.

This is the deliberate difference from the sibling tesla-reconcile/vision.py: that
tool requires the VIN to be a permanent part of the car (windshield plate / door-
jamb sticker / dash screen) and rejects a VIN on a key fob, paperwork, or a loose
label. Here we DON'T care where the VIN is — if the expected VIN is readable on ANY
surface in ANY of the photos (the key tag, a printout, the BOL, the windshield, the
dash, a sticker, a barcode label, a phone/screen — anywhere), that counts as found.
The only question is presence, not placement.

Photos are downscaled before sending (cost control). The caller (tagging.py) pre-
filters with a cheap OCR pass and sends only the CANDIDATE photos that plausibly
show the VIN — not the whole set — so this just confirms presence on what it's given.
Set VIN_CHECK_ENGINE=ocr in .env to use the on-device easyOCR reader (ocr.py) alone.
"""
from __future__ import annotations
import base64
import json
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Iterable, Optional

from PIL import Image

import config
from logging_setup import get_logger

log = get_logger(__name__)

# anthropic is imported lazily (inside _get_client) so this module imports cheaply
# and the tests — which mock vin_present_in_photos and never install anthropic —
# don't need the SDK present. Mirrors ocr.py lazy-loading easyOCR/torch.
_client: Any = None

# Send a fair-resolution frame: Claude needs enough detail to read a faint etched
# windshield VIN or a small key-fob tag, but no more. 1568px / q90 is Claude's
# high-fidelity image sweet spot (matches the sibling tool).
_MAX_SIDE = config.VISION_MAX_SIDE
_JPEG_QUALITY = config.VISION_JPEG_QUALITY


@dataclass
class VisionResult:
    """What the model reported for ONE expected VIN over a set of photos."""
    vin_present: bool
    vin_read: Optional[str] = None      # best-effort VIN read off the photos
    confidence: float = 0.0
    reasoning: str = ""                 # one short sentence on the decision
    raw: str = ""                       # raw tool input JSON, for auditing/logs


_TOOL = {
    "name": "report",
    "description": "Report whether the expected VIN is legible anywhere in the photos.",
    "input_schema": {
        "type": "object",
        "properties": {
            "vin_present": {
                "type": "boolean",
                "description": "True if the expected VIN is legibly visible ANYWHERE in "
                               "the photo set, on ANY surface — the windshield VIN plate, "
                               "a door-jamb sticker, the car's dash/touchscreen, a key or "
                               "key-fob tag, a loose printout, paperwork/BOL, a barcode or "
                               "QR label, a phone or other screen, a handwritten note — "
                               "the location does NOT matter. False only if the expected "
                               "VIN cannot be made out on any surface in any photo.",
            },
            "vin_read": {
                "type": "string",
                "description": "The VIN as read from the photos (the match you found, or "
                               "the closest VIN-like string), or empty if none legible.",
            },
            "confidence": {
                "type": "number",
                "description": "0..1 confidence that the expected VIN is legibly present "
                               "somewhere in the photos.",
            },
            "reasoning": {
                "type": "string",
                "description": "ONE short sentence: where the VIN appeared (which photo / "
                               "what surface) if found, or why it could not be confirmed "
                               "(illegible, glare, partial read, absent). Always fill this.",
            },
        },
        "required": ["vin_present", "vin_read", "confidence", "reasoning"],
    },
}

_PROMPT = (
    "You are auditing a set of vehicle PICKUP inspection photos. Your ONLY job is to "
    "decide one thing: does the expected VIN appear, legibly, ANYWHERE in these photos?\n"
    "Expected VIN: {vin}.\n"
    "WHERE the VIN appears does NOT matter. It counts if you can read it on the "
    "windshield VIN plate, a door-jamb sticker, the car's center display/touchscreen, "
    "a QR/barcode label, OR equally on a key or key-fob tag, a loose printout, the BOL "
    "or other paperwork, a phone or other screen, or a handwritten note. Any surface, "
    "attached to the car or not, is fine — this is purely a presence check.\n"
    "Read every VIN-like string you can see and compare it, character by character, to "
    "the Expected VIN (case-insensitive), paying special attention to the last 6-8 "
    "characters. Treat it as present if the legible characters agree with the Expected "
    "VIN. You do NOT need all 17 characters crisp — VIN strips and small tags are often "
    "faint, glared, or partly washed out; if the characters you CAN read are consistent "
    "with the Expected VIN and clearly the same VIN, set vin_present = true. Report what "
    "you read in vin_read.\n"
    "Set vin_present = false only when the Expected VIN genuinely cannot be made out on "
    "any surface in any photo (illegible, absent, or only a clearly different VIN is "
    "visible).\n"
    "Always fill `reasoning` with one short sentence on why you decided as you did — "
    "where the VIN was found, or what stopped it from being confirmed. Call the "
    "`report` tool with your findings."
)


def _get_client() -> Any:
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set (put it in ../secrets/.env or "
                           "this folder's .env). Set VIN_CHECK_ENGINE=ocr to use the "
                           "on-device reader instead.")
    global _client
    if _client is None:
        import anthropic  # lazy: keeps `import vision` cheap and SDK-optional
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


def vin_present_in_photos(images: Iterable[bytes], expected_vin: str) -> VisionResult:
    """Ask Claude whether `expected_vin` is legible in `images`.

    `images` is the OCR-selected candidate set (the caller pre-filters; see module
    docstring), downscaled here before upload. Returns a VisionResult. Surface is
    irrelevant — presence on the key, paperwork, or the car all count."""
    blocks: list[dict] = [{"type": "text", "text": _PROMPT.format(vin=expected_vin)}]
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
                vin_present=bool(d.get("vin_present")),
                vin_read=d.get("vin_read") or None,
                confidence=float(d.get("confidence", 0.0)),
                reasoning=(d.get("reasoning") or "").strip(),
                raw=json.dumps(d),
            )
    return VisionResult(vin_present=False, raw="no tool_use in response")
