"""Turn the three ways an event reaches us (webhook, Event History poll, manual trigger) into
one normalized shape.

Payload shapes follow the Ring docs (Notifications, Motion Detection, Event History). Event
types are kept in the webhook vocabulary (`motion_detected`, `button_press`); Event History
uses different names for the same things (`motion`, `ding`), mapped below.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

Origin = Literal["webhook", "poll", "manual"]

MANUAL_EVENT_TYPE = "manual_trigger"
HISTORY_TO_WEBHOOK_TYPE = {"motion": "motion_detected", "ding": "button_press"}


class PayloadError(ValueError):
    """The payload is authentic but not something we can store (e.g. no request_id)."""


@dataclass(frozen=True)
class NormalizedEvent:
    request_id: str
    ring_event_id: str | None
    origin: Origin
    device_id: str | None
    event_type: str
    sub_type: str | None
    occurred_at: datetime | None
    raw: dict[str, Any]


def _from_epoch_ms(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def from_webhook(payload: Any) -> NormalizedEvent:
    if not isinstance(payload, dict):
        raise PayloadError("payload is not a JSON object")
    meta = payload.get("meta") or {}
    data = payload.get("data") or {}
    if not isinstance(meta, dict) or not isinstance(data, dict):
        raise PayloadError("meta and data must be objects")
    request_id = _str_or_none(meta.get("request_id"))
    if request_id is None:
        raise PayloadError("meta.request_id is missing")
    event_type = _str_or_none(data.get("type"))
    if event_type is None:
        raise PayloadError("data.type is missing")
    attrs = data.get("attributes") or {}
    if not isinstance(attrs, dict):
        attrs = {}
    # The Motion Detection page puts the subtype at `data.subType`; other event pages (and
    # SPEC.md) use `data.attributes.sub_type`. Accept either (see FRICTION_LOG.md).
    sub_type = (
        _str_or_none(data.get("subType"))
        or _str_or_none(attrs.get("sub_type"))
        or _str_or_none(attrs.get("subType"))
    )
    return NormalizedEvent(
        request_id=request_id,
        ring_event_id=_str_or_none(data.get("id")),
        origin="webhook",
        device_id=_str_or_none(attrs.get("source")),
        event_type=event_type,
        sub_type=sub_type,
        occurred_at=_from_epoch_ms(attrs.get("timestamp")),
        raw=payload,
    )


def implied_sub_types(event_types_filter: str) -> dict[str, str]:
    """Event History items carry no detection subtype, but the `event_types` filter does: with
    `motion.human,ding` every motion item returned is a human detection. Return the subtype
    implied for each history type, when the filter pins exactly one."""
    subs: dict[str, set[str]] = {}
    for part in event_types_filter.split(","):
        kind, _, sub = part.strip().partition(".")
        if kind:
            subs.setdefault(kind, set()).add(sub)
    return {k: next(iter(v)) for k, v in subs.items() if len(v) == 1 and "" not in v}


def from_history(item: dict[str, Any], sub_types: dict[str, str]) -> NormalizedEvent:
    event_id = _str_or_none(item.get("id"))
    if event_id is None:
        raise PayloadError("history event has no id")
    attrs = item.get("attributes") or {}
    kind = attrs.get("event_type") or ""
    source = ((item.get("relationships") or {}).get("source") or {}).get("data") or {}
    return NormalizedEvent(
        # Not a webhook, so there is no request_id; derive a stable one from the event ID so
        # polling the same page twice is deduped by the same unique constraint.
        request_id=f"poll:{event_id}",
        ring_event_id=event_id,
        origin="poll",
        device_id=_str_or_none(source.get("id")),
        event_type=HISTORY_TO_WEBHOOK_TYPE.get(kind, kind),
        sub_type=sub_types.get(kind),
        occurred_at=_from_epoch_ms(attrs.get("start")),
        raw=item,
    )


def manual(device: str | None, idempotency_key: str | None, now: datetime) -> NormalizedEvent:
    key = idempotency_key or str(uuid.uuid4())
    return NormalizedEvent(
        request_id=f"manual:{key}",
        ring_event_id=None,
        origin="manual",
        device_id=device,
        event_type=MANUAL_EVENT_TYPE,
        sub_type=None,
        occurred_at=now,
        raw={"device": device, "idempotency_key": idempotency_key},
    )


def is_relevant(event: NormalizedEvent, rules: list[str]) -> bool:
    """Rules are `type` (any subtype) or `type.sub_type`. Manual triggers always run."""
    if event.origin == "manual":
        return True
    for rule in rules:
        kind, _, sub = rule.partition(".")
        if kind == event.event_type and (not sub or sub == event.sub_type):
            return True
    return False
