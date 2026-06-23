"""
Listener: validates the token (an HTTP header), dedups by a synthesized event id,
enqueues, and acks fast. No worker runs here; we assert what landed in the DB.

Super Dispatch's real webhook shape:
  * verification token  -> HTTP header `x-super-dispatch-verification-token`
  * body                -> {"action", "action_date", "order_guid"}  (no event guid)
The listener synthesizes a dedup id from action + order_guid + action_date.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import config
import db
import listener

TOKEN = "test-token-123"          # conftest sets config.SD_WEBHOOK_VERIFICATION_TOKEN to this


@pytest.fixture
def client():
    return TestClient(listener.app)


def _hdr(token=TOKEN):
    return {listener.VERIFICATION_TOKEN_HEADER: token} if token is not None else {}


def _event(action="order.picked_up", order_guid="ord-1", action_date="2026-06-22T10:00:00Z"):
    body = {"action": action, "action_date": action_date}
    if order_guid is not None:
        body["order_guid"] = order_guid
    return body


def _post(client, body, token=TOKEN):
    return client.post(config.WEBHOOK_PATH, json=body, headers=_hdr(token))


def _queue_rows():
    with db.connect() as conn:
        return conn.execute("SELECT * FROM queue").fetchall()


def test_valid_event_acks_and_enqueues(client):
    resp = _post(client, _event())
    assert resp.status_code == 200
    rows = _queue_rows()
    assert len(rows) == 1
    assert rows[0]["action"] == "order.picked_up"
    assert rows[0]["order_guid"] == "ord-1"
    assert rows[0]["status"] == "pending"


def test_bad_token_rejected_and_not_enqueued(client):
    resp = _post(client, _event(), token="WRONG")
    assert resp.status_code == 401
    assert _queue_rows() == []


def test_missing_token_rejected(client):
    resp = _post(client, _event(), token=None)   # no header at all
    assert resp.status_code == 401
    assert _queue_rows() == []


def test_duplicate_event_dropped_but_still_200(client):
    # Same action + order_guid + action_date -> same synthesized id -> SD retry.
    first = _post(client, _event())
    second = _post(client, _event())
    assert first.status_code == 200
    assert second.status_code == 200          # duplicate is success from SD's view
    assert len(_queue_rows()) == 1            # but only enqueued once


def test_unidentifiable_event_rejected(client):
    # No order_guid -> can't synthesize a dedup id -> reject so SD retries.
    resp = _post(client, _event(order_guid=None))
    assert resp.status_code == 400
    assert _queue_rows() == []


def test_malformed_json_rejected(client):
    resp = client.post(config.WEBHOOK_PATH, content=b"not json{",
                       headers={"Content-Type": "application/json", **_hdr()})
    assert resp.status_code == 400
    assert _queue_rows() == []


def test_both_pickup_status_actions_accepted(client):
    _post(client, _event(action="order.picked_up"))
    _post(client, _event(action="order.manually_marked_as_picked_up"))
    actions = {r["action"] for r in _queue_rows()}
    assert actions == {"order.picked_up", "order.manually_marked_as_picked_up"}


def test_listener_makes_no_api_calls(client, monkeypatch):
    """The listener must not touch the SD API inline — that's the worker's job."""
    import sd_client
    monkeypatch.setattr(sd_client, "_request",
                        lambda *a, **k: pytest.fail("listener called the SD API"))
    resp = _post(client, _event())
    assert resp.status_code == 200
