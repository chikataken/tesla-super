"""
Worker routing + the Playwright BOL flow. Every external layer is mocked — the SD
API, the browser (browser.browser_context), the SD web ops (sd_web.*), and the OCR
decision (tagging.decide_order_tags). No live Super Dispatch traffic, no real
browser, no torch.
"""
from __future__ import annotations
import contextlib
import json

import pytest

import browser
import config
import db
import sd_client
import sd_web
import tagging
import worker


def _enqueue(action, order_guid="ord-1", guid=None):
    guid = guid or f"evt-{action}-{order_guid}"
    payload = json.dumps({"guid": guid, "action": action, "order_guid": order_guid})
    assert db.accept_event(guid=guid, action=action, order_guid=order_guid,
                           occurred_at="2026-06-22T10:00:00Z", raw_payload=payload)


class _FakeCtx:
    def new_page(self):
        return object()                              # sd_web.* are mocked, so page is unused


@contextlib.contextmanager
def _fake_browser():
    yield _FakeCtx()


ORDER = {
    "guid": "ord-1", "number": "SHP-9001", "status": "picked_up",
    "picked_up_at": "2026-06-22T10:05:00Z",
    "vehicles": [{"vin": "VIN0000000000001"}, {"vin": "VIN0000000000002"}],
}


# --- status events (API only; must NOT open a browser) --------------------
@pytest.mark.parametrize("action", ["order.picked_up", "order.manually_marked_as_picked_up"])
def test_status_event_records_shipment_and_ui(action, monkeypatch):
    monkeypatch.setattr(sd_client, "get_order", lambda guid: ORDER)
    monkeypatch.setattr(browser, "browser_context",
                        lambda *a, **k: pytest.fail("status event opened a browser"))
    _enqueue(action)
    assert worker.run_once() is True
    with db.connect() as conn:
        ship = conn.execute("SELECT * FROM shipments WHERE order_guid='ord-1'").fetchone()
        vins = [r["vin"] for r in conn.execute("SELECT vin FROM vins WHERE order_guid='ord-1'")]
        ui = conn.execute("SELECT * FROM ui_events ORDER BY id DESC LIMIT 1").fetchone()
    assert ship["number"] == "SHP-9001"
    assert set(vins) == {"VIN0000000000001", "VIN0000000000002"}
    assert ui["kind"] == "picked_up"


# --- BOL event (Playwright path) ------------------------------------------
def _wire_bol(monkeypatch, *, decision, sections=None):
    """Mock the whole BOL flow; return a dict capturing the tags add_tags received."""
    captured = {}
    monkeypatch.setattr(sd_client, "get_order", lambda guid: ORDER)
    monkeypatch.setattr(browser, "browser_context", _fake_browser)
    monkeypatch.setattr(sd_web, "find_order_detail_url",
                        lambda page, number: "https://x/orders/view/uuid-123")
    monkeypatch.setattr(sd_web, "get_pickup_photos",
                        lambda page, url: sections if sections is not None else
                        [{"vin": "VIN0000000000001", "urls": ["https://g/p1.jpg"]},
                         {"vin": "VIN0000000000002", "urls": ["https://g/p2.jpg"]}])
    monkeypatch.setattr(sd_web, "fetch_images",
                        lambda page, urls: [b"\xff\xd8\xff\xe0jpeg-" + u.encode()[-6:] for u in urls])
    monkeypatch.setattr(sd_web, "add_tags",
                        lambda page, edit_url, tags: captured.update(edit_url=edit_url, tags=tags))
    monkeypatch.setattr(tagging, "decide_order_tags", lambda groups, vins: decision)
    return captured


def test_bol_event_all_vins_found_tags_VIN(monkeypatch):
    decision = {"vin_result": "VIN", "tags": ["VIN", "CLAUDE"], "all_found": True,
                "per_vin": {"VIN0000000000001": True, "VIN0000000000002": True},
                "photos_seen": 2, "expected": ["VIN0000000000001", "VIN0000000000002"]}
    captured = _wire_bol(monkeypatch, decision=decision)
    _enqueue("order.picked_up_bol")
    assert worker.run_once() is True

    assert captured["tags"] == ["VIN", "CLAUDE"]
    assert captured["edit_url"].endswith("/orders/edit/uuid-123")
    with db.connect() as conn:
        tag = conn.execute("SELECT * FROM tags WHERE order_guid='ord-1'").fetchone()
        photos = conn.execute("SELECT * FROM photos WHERE order_guid='ord-1'").fetchall()
        ui = conn.execute("SELECT * FROM ui_events ORDER BY id DESC LIMIT 1").fetchone()
    assert tag["vin_result"] == "VIN"
    assert json.loads(tag["applied_tags"]) == ["VIN", "CLAUDE"]
    assert len(photos) == 2                          # bytes persisted to disk + recorded
    for p in photos:
        with open(p["local_path"], "rb") as fh:
            assert fh.read().startswith(b"\xff\xd8\xff\xe0")
    assert ui["kind"] == "photos_tagged"


def test_bol_event_missing_vin_tags_NO_VIN(monkeypatch):
    decision = {"vin_result": "NO VIN", "tags": ["NO VIN", "CLAUDE"], "all_found": False,
                "per_vin": {"VIN0000000000001": True, "VIN0000000000002": False},
                "photos_seen": 2, "expected": ["VIN0000000000001", "VIN0000000000002"]}
    captured = _wire_bol(monkeypatch, decision=decision)
    _enqueue("order.picked_up_bol")
    worker.run_once()
    assert captured["tags"] == ["NO VIN", "CLAUDE"]
    with db.connect() as conn:
        assert conn.execute("SELECT vin_result FROM tags WHERE order_guid='ord-1'"
                            ).fetchone()["vin_result"] == "NO VIN"


def test_bol_event_idempotent_skips_retag(monkeypatch):
    decision = {"vin_result": "VIN", "tags": ["VIN", "CLAUDE"], "all_found": True,
                "per_vin": {}, "photos_seen": 0, "expected": []}
    calls = {"n": 0}
    _wire_bol(monkeypatch, decision=decision)
    orig_add = sd_web.add_tags
    monkeypatch.setattr(sd_web, "add_tags",
                        lambda *a, **k: calls.update(n=calls["n"] + 1))
    _enqueue("order.picked_up_bol", guid="bol-1")
    worker.run_once()
    assert calls["n"] == 1
    _enqueue("order.picked_up_bol", guid="bol-2")    # redelivered: same order, new event
    worker.run_once()
    assert calls["n"] == 1                            # not re-tagged


def test_ignored_event_routed(monkeypatch):
    monkeypatch.setattr(sd_client, "get_order",
                        lambda guid: pytest.fail("ignored event should not fetch order"))
    _enqueue(config.PICKUP_IGNORED_ACTION)
    assert worker.run_once() is True
    with db.connect() as conn:
        ui = conn.execute("SELECT * FROM ui_events ORDER BY id DESC LIMIT 1").fetchone()
    assert ui["kind"] == "picked_up_ignored"


# --- failure handling -----------------------------------------------------
def test_failed_item_retries_then_parks(monkeypatch):
    monkeypatch.setattr(sd_client, "get_order",
                        lambda guid: (_ for _ in ()).throw(sd_client.SDError("boom")))
    _enqueue("order.picked_up")
    for _ in range(3):                               # max_attempts=3 (conftest)
        worker.run_once()
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM queue").fetchone()
    assert row["status"] == "failed"
    assert row["attempts"] == 3
    assert "boom" in (row["last_error"] or "")


def test_empty_queue_returns_false():
    assert worker.run_once() is False
