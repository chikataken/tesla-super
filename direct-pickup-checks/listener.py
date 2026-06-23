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

import config
import db
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


def extract_fields(payload: dict) -> dict:
    """Pull (token, guid, action, order_guid, occurred_at) from a webhook payload.

    VERIFY field names. Known from the brief: `verification_token`, event `guid`,
    `occurred_at`, target object `order_guid`. The action-name field is not stated;
    we try the common spellings."""
    return {
        "token": _first(payload, "verification_token"),
        "guid": _first(payload, "guid", "event_guid", "id"),
        "action": _first(payload, "action", "event", "event_type", "type"),
        "order_guid": _first(payload, "order_guid", "object_guid", "target_guid"),
        "occurred_at": _first(payload, "occurred_at", "created_at", "timestamp"),
    }


def _token_ok(token: Optional[str]) -> bool:
    expected = config.SD_WEBHOOK_VERIFICATION_TOKEN
    if not expected:
        log.error("SD_WEBHOOK_VERIFICATION_TOKEN not set — rejecting all webhooks")
        return False
    return bool(token) and hmac.compare_digest(token, expected)


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

    # (1) authenticate
    if not _token_ok(f["token"]):
        log.warning("verification_token mismatch", extra={"action": f["action"]})
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
    if accepted:
        log.info("accepted", extra={"guid": f["guid"], "action": f["action"],
                                    "order_guid": f["order_guid"]})
    else:
        log.info("duplicate dropped", extra={"guid": f["guid"], "action": f["action"]})

    # (4) ack fast. 200 whether new or duplicate — a duplicate is success from SD's
    # perspective (we already have it), and acking stops the retry storm.
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
