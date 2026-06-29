"""
The per-shipment tag decision, for BOTH VIN-check engines.

  * engine="ocr"    -> ocr.scan_for_vin(images, vin) is mocked (no easyOCR/torch);
                       returns a list of matching photo indices, non-empty = found.
  * engine="claude" -> vision.vin_present_in_photos(pool, vin) is mocked (no anthropic
                       SDK / no API calls); returns a VisionResult with .vin_present.

We test the decision logic and the per-engine wiring, not the engines themselves.
"""
from __future__ import annotations

import ocr
import tagging
import vision


def _groups():
    # two vehicles, each with its own pickup photos
    return [
        {"vin": "VIN0000000000001", "images": [b"a1", b"a2"]},
        {"vin": "VIN0000000000002", "images": [b"b1"]},
    ]


def _vres(present: bool) -> vision.VisionResult:
    return vision.VisionResult(vin_present=present, vin_read="x", confidence=1.0)


# --------------------------- OCR engine ---------------------------
def test_all_vins_found_tags_VIN(monkeypatch):
    monkeypatch.setattr(ocr, "scan_for_vin", lambda images, vin, **k: [0])   # always found
    result = tagging.decide_order_tags(_groups(), ["VIN0000000000001", "VIN0000000000002"],
                                       engine="ocr")
    assert result["all_found"] is True
    assert result["vin_result"] == "VIN"
    assert result["tags"][0] == "VIN"
    assert result["per_vin"] == {"VIN0000000000001": True, "VIN0000000000002": True}
    assert result["photos_seen"] == 3
    assert result["engine"] == "ocr"


def test_one_vin_missing_tags_NO_VIN(monkeypatch):
    def scan(images, vin, **k):
        return [0] if vin == "VIN0000000000001" else []      # second VIN not found
    monkeypatch.setattr(ocr, "scan_for_vin", scan)
    result = tagging.decide_order_tags(_groups(), ["VIN0000000000001", "VIN0000000000002"],
                                       engine="ocr")
    assert result["all_found"] is False
    assert result["vin_result"] == "NO VIN"
    assert result["tags"][0] == "NO VIN"


def test_no_expected_vins_tags_NO_VIN(monkeypatch):
    monkeypatch.setattr(ocr, "scan_for_vin", lambda *a, **k: [0])
    result = tagging.decide_order_tags(_groups(), [], engine="ocr")
    assert result["vin_result"] == "NO VIN"          # nothing to confirm -> NO VIN
    assert result["all_found"] is False


def test_ocr_error_is_treated_as_not_found(monkeypatch):
    def boom(images, vin, **k):
        raise RuntimeError("ocr exploded")
    monkeypatch.setattr(ocr, "scan_for_vin", boom)
    result = tagging.decide_order_tags(_groups(), ["VIN0000000000001"], engine="ocr")
    assert result["vin_result"] == "NO VIN"          # never crashes; degrades to NO VIN


def test_vin_scanned_against_its_own_section(monkeypatch):
    """OCR scans each VIN against its own section's images (fallback to the full pool
    only if the section isn't identifiable)."""
    seen = {}
    monkeypatch.setattr(ocr, "scan_for_vin",
                        lambda images, vin, **k: seen.setdefault(vin, images) and [0])
    tagging.decide_order_tags(_groups(), ["VIN0000000000001", "VIN0000000000002"],
                              engine="ocr")
    assert seen["VIN0000000000001"] == [b"a1", b"a2"]
    assert seen["VIN0000000000002"] == [b"b1"]


# --------------------------- Claude engine ---------------------------
# The claude engine OCR-pre-filters the pool (ocr.scan_for_vin) and sends ONLY the
# candidate photos to Claude (vision.vin_present_in_photos). Both are mocked here:
# scan_for_vin returns candidate photo INDICES, vision confirms presence.

def _all_candidates(images, vin, **k):
    return list(range(len(images)))          # every photo is a candidate


def test_claude_is_the_default_engine(monkeypatch):
    """With no explicit engine, decide_order_tags follows config.VIN_CHECK_ENGINE."""
    monkeypatch.setattr(tagging.config, "VIN_CHECK_ENGINE", "claude")
    monkeypatch.setattr(ocr, "scan_for_vin", _all_candidates)
    calls = []
    monkeypatch.setattr(vision, "vin_present_in_photos",
                        lambda imgs, vin: calls.append(vin) or _vres(True))
    result = tagging.decide_order_tags(_groups(), ["VIN0000000000001"])
    assert result["engine"] == "claude"
    assert result["vin_result"] == "VIN"
    assert calls == ["VIN0000000000001"]


def test_claude_all_present_tags_VIN(monkeypatch):
    monkeypatch.setattr(ocr, "scan_for_vin", _all_candidates)
    monkeypatch.setattr(vision, "vin_present_in_photos", lambda imgs, vin: _vres(True))
    result = tagging.decide_order_tags(_groups(), ["VIN0000000000001", "VIN0000000000002"],
                                       engine="claude")
    assert result["all_found"] is True
    assert result["vin_result"] == "VIN"
    assert result["per_vin"] == {"VIN0000000000001": True, "VIN0000000000002": True}


def test_claude_one_absent_tags_NO_VIN(monkeypatch):
    monkeypatch.setattr(ocr, "scan_for_vin", _all_candidates)
    monkeypatch.setattr(vision, "vin_present_in_photos",
                        lambda imgs, vin: _vres(vin == "VIN0000000000001"))
    result = tagging.decide_order_tags(_groups(), ["VIN0000000000001", "VIN0000000000002"],
                                       engine="claude")
    assert result["all_found"] is False
    assert result["vin_result"] == "NO VIN"


def test_claude_only_sends_ocr_candidate_photos(monkeypatch):
    """Claude must receive ONLY the photos the OCR pre-filter selected from the whole
    pool — not the entire set (cost control), and any surface/section is eligible."""
    # pool order is [a1, a2, b1]; pre-filter keeps photos 0 and 2 for this VIN.
    monkeypatch.setattr(ocr, "scan_for_vin", lambda images, vin, **k: [0, 2])
    sent = {}
    monkeypatch.setattr(vision, "vin_present_in_photos",
                        lambda imgs, vin: sent.setdefault(vin, imgs) or _vres(True))
    tagging.decide_order_tags(_groups(), ["VIN0000000000001"], engine="claude")
    assert sent["VIN0000000000001"] == [b"a1", b"b1"]      # only the candidates


def test_claude_no_candidates_skips_the_api(monkeypatch):
    """If the OCR pre-filter finds no candidate photos, Claude is NOT called and the
    VIN is treated as not found (cheapest path -> NO VIN)."""
    monkeypatch.setattr(ocr, "scan_for_vin", lambda images, vin, **k: [])   # no candidates
    called = []
    monkeypatch.setattr(vision, "vin_present_in_photos",
                        lambda imgs, vin: called.append(vin) or _vres(True))
    result = tagging.decide_order_tags(_groups(), ["VIN0000000000001"], engine="claude")
    assert called == []                               # API never hit
    assert result["vin_result"] == "NO VIN"


def test_claude_error_is_treated_as_not_found(monkeypatch):
    monkeypatch.setattr(ocr, "scan_for_vin", _all_candidates)
    def boom(imgs, vin):
        raise RuntimeError("api exploded")
    monkeypatch.setattr(vision, "vin_present_in_photos", boom)
    result = tagging.decide_order_tags(_groups(), ["VIN0000000000001"], engine="claude")
    assert result["vin_result"] == "NO VIN"          # never crashes; degrades to NO VIN
