import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.ingest import events, signature

DOCS = Path(__file__).parent / "fixtures" / "ring" / "docs"


def webhook_payload() -> dict:
    return json.loads((DOCS / "webhook_motion_human.json").read_text())["payload"]


def history_items() -> list[dict]:
    return json.loads((DOCS / "history_events.json").read_text())["response"]["data"]


# --- signature ---------------------------------------------------------------------------


def test_signature_round_trip():
    body = b'{"a": 1}'
    header = signature.sign("key", body)
    assert header.startswith("sha256=") and len(header) == 7 + 64
    assert signature.verify("key", body, header)


def test_signature_matches_docs_algorithm():
    import hashlib
    import hmac

    body = b'{"meta": {}}'
    expected = hmac.new(b"k\xc3\xa9y", body, hashlib.sha256).hexdigest()
    # Key is used as UTF-8 bytes, not Base64-decoded.
    assert signature.verify("kéy", body, "sha256=" + expected)


@pytest.mark.parametrize(
    "header",
    [None, "", "sha256=", "deadbeef", "sha1=" + "0" * 40, "sha256=" + "0" * 64],
)
def test_signature_rejects_bad_headers(header):
    assert not signature.verify("key", b"body", header)


def test_signature_rejects_tampered_body_and_wrong_key():
    header = signature.sign("key", b'{"n": 1}')
    assert not signature.verify("key", b'{"n": 2}', header)
    assert not signature.verify("other", b'{"n": 1}', header)
    # Re-serialized JSON (different whitespace) must not verify: only raw bytes are signed.
    assert not signature.verify("key", b'{"n":1}', header)


def test_signature_accepts_uppercase_hex():
    header = signature.sign("key", b"x")
    assert signature.verify("key", b"x", "sha256=" + header[7:].upper())


# --- webhook parsing ---------------------------------------------------------------------


def test_from_webhook_docs_example():
    ev = events.from_webhook(webhook_payload())
    assert ev.request_id == "req-0001"
    assert ev.ring_event_id == "evt-0001"
    assert ev.origin == "webhook"
    assert ev.device_id == "ava1.ring.device.TEST"
    assert ev.event_type == "motion_detected"
    assert ev.sub_type == "human"
    assert ev.occurred_at == datetime(2026, 10, 16, 17, 5, tzinfo=UTC)


def test_from_webhook_reads_sub_type_from_attributes_too():
    p = webhook_payload()
    del p["data"]["subType"]
    p["data"]["attributes"]["sub_type"] = "vehicle"
    assert events.from_webhook(p).sub_type == "vehicle"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["meta"].pop("request_id"),
        lambda p: p["meta"].__setitem__("request_id", ""),
        lambda p: p["data"].pop("type"),
        lambda p: p.pop("meta"),
    ],
)
def test_from_webhook_rejects_missing_keys(mutate):
    p = webhook_payload()
    mutate(p)
    with pytest.raises(events.PayloadError):
        events.from_webhook(p)


def test_from_webhook_tolerates_missing_timestamp_and_source():
    p = webhook_payload()
    p["data"]["attributes"] = {}
    ev = events.from_webhook(p)
    assert ev.occurred_at is None and ev.device_id is None


def test_from_webhook_rejects_non_object():
    with pytest.raises(events.PayloadError):
        events.from_webhook([1, 2])


# --- history and manual ------------------------------------------------------------------


def test_implied_sub_types():
    assert events.implied_sub_types("motion.human,ding") == {"motion": "human"}
    # Two motion subtypes: the history item cannot say which one it was.
    assert events.implied_sub_types("motion.human,motion.vehicle") == {}
    assert events.implied_sub_types("motion,motion.human") == {}


def test_from_history_maps_to_webhook_vocabulary():
    subs = events.implied_sub_types("motion.human,ding")
    ding, motion = (events.from_history(i, subs) for i in history_items())
    assert motion.event_type == "motion_detected" and motion.sub_type == "human"
    assert ding.event_type == "button_press" and ding.sub_type is None
    assert motion.request_id == "poll:hist-motion-1"
    assert motion.ring_event_id == "hist-motion-1"
    assert motion.device_id == "ava1.ring.device.TEST"
    assert motion.occurred_at == datetime(2026, 10, 16, 17, 5, tzinfo=UTC)


def test_from_history_requires_id():
    item = copy.deepcopy(history_items()[0])
    del item["id"]
    with pytest.raises(events.PayloadError):
        events.from_history(item, {})


def test_manual_events_are_keyed_and_always_relevant():
    now = datetime(2026, 10, 16, tzinfo=UTC)
    a = events.manual("Back Door", "k1", now)
    assert a.request_id == "manual:k1"
    assert events.manual(None, None, now).request_id != events.manual(None, None, now).request_id
    assert events.is_relevant(a, [])


def test_relevance_rules():
    ev = events.from_webhook(webhook_payload())  # motion_detected.human
    assert events.is_relevant(ev, ["motion_detected.human"])
    assert events.is_relevant(ev, ["motion_detected"])
    assert not events.is_relevant(ev, ["motion_detected.vehicle", "button_press"])
    p = webhook_payload()
    p["data"]["type"] = "device_online"
    assert not events.is_relevant(events.from_webhook(p), ["motion_detected", "button_press"])
