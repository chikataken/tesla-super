"""
Webhook listener — FastAPI under uvicorn, bound to loopback.

It does the BARE MINIMUM per request and acks fast, because Super Dispatch times
out at 10s and expects a 2xx within 60s (then retries 5× at 60s intervals). So on
each POST it only:
    1. validates the `verification_token` (constant-time);
    2. dedups on the event `guid` (seen_events table);
    3. persists the raw payload to the durable queue;
    4. returns 200 immediately.
NO API calls, NO photo work, NO heavy DB logic inline — that's all the worker's job.

It also exposes a Server-Sent-Events feed (`GET <WEBHOOK_PATH>/../events/stream`,
i.e. /events/stream) that tails the ui_events table the worker appends to, so a UI
in another process gets live pickup / photos-tagged updates.

Run (dev):  uvicorn listener:app --host 127.0.0.1 --port 8077
Prod:       systemd runs uvicorn (see systemd/direct-pickup-listener.service)
"""
from __future__ import annotations
import asyncio
import hmac
import json
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

import config
import db
import sd_client
import recorder_sink
from logging_setup import setup, get_logger

setup("listener")
log = get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    db.init_db()
    log.info("listener up", extra={"path": config.WEBHOOK_PATH,
                                   "bind": f"{config.LISTENER_HOST}:{config.LISTENER_PORT}"})
    yield


app = FastAPI(title="direct-pickup-checks listener", lifespan=_lifespan)


# --------------------------------------------------------------------------
# Payload field extraction. The payload carries default fields + the target GUID,
# NOT the full shipment. Field NAMES are isolated here — VERIFY against the live
# reference and adjust the candidate lists if your tenant differs.
# --------------------------------------------------------------------------
def _first(d: dict, *keys: str) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return str(v)
    return None


# Super Dispatch sends the verification token as an HTTP HEADER (not in the body).
VERIFICATION_TOKEN_HEADER = "x-super-dispatch-verification-token"


def extract_fields(payload: dict) -> dict:
    """Pull (guid, action, order_guid, occurred_at) from a webhook payload.

    SD's real payload is just {action, action_date, order_guid} with NO event guid
    (the token is a header, handled separately). So we synthesize a stable dedup id
    from action + order_guid + action_date — identical across SD's retries of one
    event, distinct across different events."""
    action = _first(payload, "action", "event", "event_type", "type")
    order_guid = _first(payload, "order_guid", "object_guid", "target_guid")
    occurred_at = _first(payload, "action_date", "occurred_at", "created_at", "timestamp")
    guid = _first(payload, "guid", "event_guid", "id")
    if not guid and action and order_guid:
        guid = f"{action}:{order_guid}:{occurred_at or ''}"
    return {"guid": guid, "action": action, "order_guid": order_guid,
            "occurred_at": occurred_at}


# --------------------------------------------------------------------------
# Recorder fan-out. Independent of the direct-pickup queue/worker: for ANY accepted
# event we fetch the full order and mirror it into the shipment-recorder DB, so the
# Recorded page live-updates. Runs as a Starlette BackgroundTask (sync fn → threadpool)
# AFTER the 200 is sent, and is fully wrapped so a recorder failure never affects the
# ack or the pickup pipeline.
# --------------------------------------------------------------------------
def _recorder_fanout(order_guid: Optional[str], action: Optional[str],
                     occurred_at: Optional[str], payload_text: str) -> None:
    if not order_guid:
        return
    try:
        order = None
        try:
            order = sd_client.get_order(order_guid)
        except Exception as e:                               # noqa: BLE001
            log.warning("recorder fanout get_order failed",
                        extra={"order_guid": order_guid, "err": str(e)[:200]})
        recorder_sink.record_and_upsert(order_guid, action, occurred_at, payload_text, order)
        log.info("recorder updated", extra={"order_guid": order_guid, "action": action,
                                            "status": recorder_sink.derive_status(order) if order else None})
    except Exception as e:                                   # noqa: BLE001
        log.warning("recorder fanout failed", extra={"order_guid": order_guid, "err": str(e)[:200]})


def _token_ok(token: Optional[str]) -> bool:
    # Build the valid set at CALL time from both config sources (so tests that patch
    # the single token, and prod that sets the multi-token set, both work).
    valid = set(config.SD_WEBHOOK_VERIFICATION_TOKENS)
    if config.SD_WEBHOOK_VERIFICATION_TOKEN:
        valid.add(config.SD_WEBHOOK_VERIFICATION_TOKEN)
    if not valid:
        log.error("no verification tokens configured — rejecting all webhooks")
        return False
    # Super Dispatch sends a per-action token; accept if it matches any of ours.
    # compare_digest against each keeps the check constant-time per candidate.
    return bool(token) and any(hmac.compare_digest(token, v) for v in valid)


@app.post(config.WEBHOOK_PATH)
async def webhook(request: Request) -> Response:
    raw = await request.body()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
    except (ValueError, json.JSONDecodeError):
        # Malformed body: 400 (not 2xx) so SD doesn't think we accepted garbage.
        log.warning("bad payload", extra={"bytes": len(raw)})
        return Response(status_code=400, content="invalid JSON")

    f = extract_fields(payload)

    # (1) authenticate — Super Dispatch sends the token as an HTTP header.
    token = request.headers.get(VERIFICATION_TOKEN_HEADER)
    if not _token_ok(token):
        log.warning("verification_token mismatch", extra={
            "action": f["action"],
            "header_present": VERIFICATION_TOKEN_HEADER in request.headers,
            "valid_tokens_loaded": len(config.SD_WEBHOOK_VERIFICATION_TOKENS),
        })
        return Response(status_code=401, content="bad verification token")

    # An event with no guid can't be deduped safely — reject so SD retries rather
    # than us silently processing it twice. (VERIFY the guid field name if this trips.)
    if not f["guid"]:
        log.warning("event missing guid", extra={"action": f["action"]})
        return Response(status_code=400, content="missing event guid")

    # (2)+(3) dedup + enqueue, atomically
    raw_text = raw.decode("utf-8", "replace")
    accepted = await asyncio.to_thread(
        db.accept_event,
        guid=f["guid"], action=f["action"] or "", order_guid=f["order_guid"],
        occurred_at=f["occurred_at"], raw_payload=raw_text,
    )
    # (4) ack fast. 200 whether new or duplicate — a duplicate is success from SD's
    # perspective (we already have it), and acking stops the retry storm. On a NEW
    # event, fan out to the recorder AFTER the response (BackgroundTask), so the
    # Recorded mirror updates live without slowing the ack.
    if accepted:
        log.info("accepted", extra={"guid": f["guid"], "action": f["action"],
                                    "order_guid": f["order_guid"]})
        return Response(status_code=200, content="ok",
                        background=BackgroundTask(_recorder_fanout, f["order_guid"],
                                                  f["action"], f["occurred_at"], raw_text))
    log.info("duplicate dropped", extra={"guid": f["guid"], "action": f["action"]})
    return Response(status_code=200, content="ok")


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


# --------------------------------------------------------------------------
# UI push: SSE feed tailing ui_events (the worker appends; the UI subscribes).
# Cross-process: worker writes rows to SQLite, this streams them out.
# --------------------------------------------------------------------------
@app.get("/events/stream")
async def events_stream(request: Request, after: int = 0):
    async def gen():
        last = after
        # Send a comment immediately so proxies/clients open the stream.
        yield ": connected\n\n"
        while True:
            if await request.is_disconnected():
                return
            rows = await asyncio.to_thread(db.ui_events_after, last, 100)
            for r in rows:
                last = r["id"]
                data = {"id": r["id"], "order_guid": r["order_guid"], "kind": r["kind"],
                        "payload": json.loads(r["payload"] or "{}"),
                        "created_at": r["created_at"]}
                yield f"id: {r['id']}\nevent: {r['kind']}\ndata: {json.dumps(data)}\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
