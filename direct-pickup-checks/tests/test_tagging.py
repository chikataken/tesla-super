"""
The per-shipment tag decision. ocr.scan_for_vin is mocked so easyOCR/torch never
load — we test the decision logic, not the OCR engine itself.

scan_for_vin(images, vin) returns a list of matching photo indices; non-empty means
"this VIN was found in these photos".
"""
from __future__ import annotations

import ocr
import tagging


def _groups():
    # two vehicles, each with its own pickup photos
    return [
        {"vin": "VIN0000000000001", "images": [b"a1", b"a2"]},
        {"vin": "VIN0000000000002", "images": [b"b1"]},
    ]


def test_all_vins_found_tags_VIN(monkeypatch):
    monkeypatch.setattr(ocr, "scan_for_vin", lambda images, vin, **k: [0])   # always found
    result = tagging.decide_order_tags(_groups(), ["VIN0000000000001", "VIN0000000000002"])
    assert result["all_found"] is True
    assert result["vin_result"] == "VIN"
    assert result["tags"] == ["VIN", "CLAUDE"]
    assert result["per_vin"] == {"VIN0000000000001": True, "VIN0000000000002": True}
    assert result["photos_seen"] == 3


def test_one_vin_missing_tags_NO_VIN(monkeypatch):
    def scan(images, vin, **k):
        return [0] if vin == "VIN0000000000001" else []      # second VIN not found
    monkeypatch.setattr(ocr, "scan_for_vin", scan)
    result = tagging.decide_order_tags(_groups(), ["VIN0000000000001", "VIN0000000000002"])
    assert result["all_found"] is False
    assert result["vin_result"] == "NO VIN"
    assert result["tags"] == ["NO VIN", "CLAUDE"]


def test_no_expected_vins_tags_NO_VIN(monkeypatch):
    monkeypatch.setattr(ocr, "scan_for_vin", lambda *a, **k: [0])
    result = tagging.decide_order_tags(_groups(), [])
    assert result["vin_result"] == "NO VIN"          # nothing to confirm -> NO VIN
    assert result["all_found"] is False


def test_ocr_error_is_treated_as_not_found(monkeypatch):
    def boom(images, vin, **k):
        raise RuntimeError("ocr exploded")
    monkeypatch.setattr(ocr, "scan_for_vin", boom)
    result = tagging.decide_order_tags(_groups(), ["VIN0000000000001"])
    assert result["vin_result"] == "NO VIN"          # never crashes; degrades to NO VIN


def test_vin_scanned_against_its_own_section(monkeypatch):
    """Each VIN should be scanned against its own section's images (fallback to the
    full pool only if the section isn't identifiable)."""
    seen = {}
    monkeypatch.setattr(ocr, "scan_for_vin",
                        lambda images, vin, **k: seen.setdefault(vin, images) and [0])
    tagging.decide_order_tags(_groups(), ["VIN0000000000001", "VIN0000000000002"])
    assert seen["VIN0000000000001"] == [b"a1", b"a2"]
    assert seen["VIN0000000000002"] == [b"b1"]
