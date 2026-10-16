"""HTTP entry points: the Ring webhook and the manual trigger.

Both only verify, store and wake the queue; all real work happens in the workers, so the
webhook acks in a few milliseconds (Ring requires a response within 5 s).
"""

import hmac
import json
import logging
import time

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.ingest import events, signature
from app.ingest.queue import insert_event, utcnow

log = logging.getLogger(__name__)

router = APIRouter()


async def _store(request: Request, event: events.NormalizedEvent) -> tuple[str, str | None]:
    state = request.app.state
    relevant = events.is_relevant(event, state.settings.ingest_relevant_events)
    async with state.engine.begin() as conn:
        new_id = await insert_event(conn, event, relevant, received_at=utcnow())
    if new_id is None:
        return "duplicate", None
    if relevant:
        state.queue.notify()
        return "queued", str(new_id)
    return "ignored", str(new_id)


@router.post("/webhooks/ring")
async def ring_webhook(
    request: Request, x_signature: str | None = Header(default=None)
) -> JSONResponse:
    t0 = time.perf_counter()
    secret = request.app.state.settings.ring_webhook_secret
    if secret is None:
        # Never accept unsigned events, even in development.
        raise HTTPException(503, "webhook signing key is not configured")
    # Verify against the raw bytes; re-serialized JSON would not match the signature.
    body = await request.body()
    if not signature.verify(secret.get_secret_value(), body, x_signature):
        raise HTTPException(401, "invalid signature")
    try:
        event = events.from_webhook(json.loads(body))
    except (ValueError, events.PayloadError) as e:
        # Authentic but unusable; a retry would not fix it.
        raise HTTPException(400, str(e)) from None
    status, event_id = await _store(request, event)
    log.info(
        "webhook %s %s.%s -> %s in %.1f ms",
        event.request_id,
        event.event_type,
        event.sub_type,
        status,
        (time.perf_counter() - t0) * 1000,
    )
    return JSONResponse({"status": status, "event_id": event_id})


class TriggerRequest(BaseModel):
    device: str | None = Field(
        default=None, description="Device name or ID; defaults to the only camera"
    )
    idempotency_key: str | None = Field(
        default=None, max_length=200, description="Repeat a trigger safely with the same key"
    )


@router.post("/v1/trigger", status_code=202)
async def manual_trigger(
    request: Request, body: TriggerRequest, authorization: str | None = Header(default=None)
) -> JSONResponse:
    """Start the pipeline as if a delivery-relevant event had just happened on the device.

    For the Playground demo (no webhooks) and for re-running capture by hand. Requires
    `Authorization: Bearer $ADMIN_TOKEN`; disabled when ADMIN_TOKEN is unset.
    """
    admin = request.app.state.settings.admin_token
    if admin is None:
        raise HTTPException(404, "manual trigger is disabled")
    expected = f"Bearer {admin.get_secret_value()}"
    if not hmac.compare_digest((authorization or "").encode(), expected.encode()):
        raise HTTPException(401, "invalid admin token")
    event = events.manual(body.device, body.idempotency_key, utcnow())
    status, event_id = await _store(request, event)
    return JSONResponse({"status": status, "event_id": event_id}, status_code=202)
