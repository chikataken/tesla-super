"""
Listener: validates the token, dedups by event guid, enqueues, and acks fast.
No worker runs here; we assert what landed in the DB.
"""
from __future__ import annotations
import json

import pytest
from fastapi.testclient import TestClient

import config
import db
import listener


@pytest.fixture
def client():
    return TestClient(listener.app)


def _event(guid="evt-1", action="order.picked_up", order_guid="ord-1", token="test-token-123"):
    return {
        "verification_token": token,
        "guid": guid,
        "action": action,
        "order_guid": order_guid,
        "occurred_at": "2026-06-22T10:00:00Z",
    }


def _queue_rows():
    with db.connect() as conn:
        return conn.execute("SELECT * FROM queue").fetchall()


def test_valid_event_acks_and_enqueues(client):
    resp = client.post(config.WEBHOOK_PATH, json=_event())
    assert resp.status_code == 200
    rows = _queue_rows()
    assert len(rows) == 1
    assert rows[0]["action"] == "order.picked_up"
    assert rows[0]["order_guid"] == "ord-1"
    assert rows[0]["status"] == "pending"


def test_bad_token_rejected_and_not_enqueued(client):
    resp = client.post(config.WEBHOOK_PATH, json=_event(token="WRONG"))
    assert resp.status_code == 401
    assert _queue_rows() == []


def test_missing_token_rejected(client):
    payload = _event()
    del payload["verification_token"]
    resp = client.post(config.WEBHOOK_PATH, json=payload)
    assert resp.status_code == 401
    assert _queue_rows() == []


def test_duplicate_guid_dropped_but_still_200(client):
    first = client.post(config.WEBHOOK_PATH, json=_event(guid="dup"))
    second = client.post(config.WEBHOOK_PATH, json=_event(guid="dup"))
    assert first.status_code == 200
    assert second.status_code == 200          # duplicate is success from SD's view
    assert len(_queue_rows()) == 1            # but only enqueued once


def test_missing_guid_rejected(client):
    payload = _event()
    del payload["guid"]
    resp = client.post(config.WEBHOOK_PATH, json=payload)
    assert resp.status_code == 400
    assert _queue_rows() == []


def test_malformed_json_rejected(client):
    resp = client.post(config.WEBHOOK_PATH, content=b"not json{",
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    assert _queue_rows() == []


def test_both_pickup_status_actions_accepted(client):
    client.post(config.WEBHOOK_PATH, json=_event(guid="a", action="order.picked_up"))
    client.post(config.WEBHOOK_PATH,
                json=_event(guid="b", action="order.manually_marked_as_picked_up"))
    actions = {r["action"] for r in _queue_rows()}
    assert actions == {"order.picked_up", "order.manually_marked_as_picked_up"}


def test_listener_makes_no_api_calls(client, monkeypatch):
    """The listener must not touch the SD API inline — that's the worker's job."""
    import sd_client
    monkeypatch.setattr(sd_client, "_request",
                        lambda *a, **k: pytest.fail("listener called the SD API"))
    resp = client.post(config.WEBHOOK_PATH, json=_event())
    assert resp.status_code == 200
