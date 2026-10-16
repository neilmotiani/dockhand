"""Postgres schema (SPEC.md section 4), as SQLAlchemy Core tables.

Core rather than the ORM: every write in the pipeline is an idempotent upsert
(`INSERT ... ON CONFLICT`), which reads more plainly as a statement than as ORM session state.

Additions to the SPEC columns, all in service of idempotency or the queue:
- ring_events.ring_event_id: Ring's own event ID (`data.id` in a webhook, `data[].id` in Event
  History). Unique, so the poller and a webhook reporting the same event cannot both enqueue it.
- ring_events.origin/status/attempts/claimed_at/...: the table doubles as the durable work queue.
- deliveries.event_id is unique: one delivery per event, however often the event is replayed.
- extraction_cache: Bedrock results keyed by a hash of the frames, model and prompt version.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

metadata = MetaData()

EVENT_STATUSES = ("pending", "processing", "done", "ignored", "skipped", "failed")
ORIGINS = ("webhook", "poll", "manual")
SOURCES = ("ring_playground", "ring_device", "own_footage")
RECONCILIATION_STATUSES = ("matched", "short", "over", "missing", "unexpected")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} in ({', '.join(repr(v) for v in values)})"


ring_events = Table(
    "ring_events",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("request_id", Text, nullable=False, unique=True),
    Column("ring_event_id", Text, unique=True),
    Column("origin", Text, nullable=False),
    Column("device_id", Text),
    Column("event_type", Text, nullable=False),
    Column("sub_type", Text),
    Column("occurred_at", DateTime(timezone=True)),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("raw", JSONB, nullable=False),
    Column("relevant", Boolean, nullable=False),
    Column("status", Text, nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("claimed_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("outcome", Text),
    Column("last_error", Text),
    CheckConstraint(_in("status", EVENT_STATUSES), name="ring_events_status"),
    CheckConstraint(_in("origin", ORIGINS), name="ring_events_origin"),
)
# The worker's claim query scans only pending/processing rows.
Index(
    "ring_events_queue",
    ring_events.c.received_at,
    postgresql_where=ring_events.c.status.in_(("pending", "processing")),
)

deliveries = Table(
    "deliveries",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("event_id", UUID(as_uuid=True), ForeignKey("ring_events.id"), unique=True),
    Column("device_id", Text),
    Column("detected_at", DateTime(timezone=True), nullable=False),
    Column("carrier", Text, nullable=False),
    Column("package_count", Integer, nullable=False),
    Column("label_text", ARRAY(Text), nullable=False),
    Column("confidence", Float),
    Column("reasoning", Text),
    Column("snapshot_path", Text),
    Column("clip_path", Text),
    Column("model_id", Text),
    Column("prompt_version", Text),
    Column("frames_sampled", Integer),
    Column("frames_sent", Integer),
    Column("usage", JSONB),
    Column("latency_ms", JSONB, nullable=False),
    Column("source", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(_in("source", SOURCES), name="deliveries_source"),
)
Index("deliveries_detected_at", deliveries.c.detected_at)

expected_deliveries = Table(
    "expected_deliveries",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("supplier", Text, nullable=False),
    Column("carrier", Text),
    Column("expected_count", Integer, nullable=False),
    Column("window_start", DateTime(timezone=True), nullable=False),
    Column("window_end", DateTime(timezone=True), nullable=False),
    Column("reference", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("expected_count >= 1", name="expected_count_positive"),
    CheckConstraint("window_end >= window_start", name="expected_window_ordered"),
)

reconciliations = Table(
    "reconciliations",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("expected_id", UUID(as_uuid=True), ForeignKey("expected_deliveries.id")),
    Column("delivery_id", UUID(as_uuid=True), ForeignKey("deliveries.id")),
    Column("status", Text, nullable=False),
    Column("score", Float),
    Column("reason", Text, nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(_in("status", RECONCILIATION_STATUSES), name="reconciliations_status"),
    CheckConstraint(
        "expected_id is not null or delivery_id is not null", name="reconciliations_has_side"
    ),
)

extraction_cache = Table(
    "extraction_cache",
    metadata,
    Column("key", Text, primary_key=True),
    Column("model_id", Text, nullable=False),
    Column("prompt_version", Text, nullable=False),
    Column("outcome", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
