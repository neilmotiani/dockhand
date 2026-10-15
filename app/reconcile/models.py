"""Inputs and outputs of the reconciliation engine.

These are plain in-memory models, independent of the database layer, so the matcher is a pure
function that can be unit-tested and replayed by eval/reconciliation.py without Postgres.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

# Fixed namespace so reconciliation IDs are a deterministic function of the (expected, delivery)
# pair. Re-running reconciliation produces the same IDs, so an upsert never duplicates a row.
RECONCILIATION_NAMESPACE = uuid.UUID("6f1c2a52-4d0e-4b8e-9a51-1d3f0c7e2b90")

UNKNOWN_CARRIERS = frozenset({"unknown", ""})


class Status(StrEnum):
    MATCHED = "matched"
    SHORT = "short"
    OVER = "over"
    MISSING = "missing"
    UNEXPECTED = "unexpected"


DISCREPANCY_STATUSES = frozenset({Status.SHORT, Status.OVER, Status.MISSING, Status.UNEXPECTED})


class DetectedDelivery(BaseModel):
    """A delivery seen on camera (one row of `deliveries`)."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    detected_at: AwareDatetime
    carrier: str
    package_count: int = Field(ge=0)


class ExpectedDelivery(BaseModel):
    """A delivery the business told us to expect (one row of `expected_deliveries`)."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    supplier: str
    carrier: str | None = None
    expected_count: int = Field(ge=1)
    window_start: AwareDatetime
    window_end: AwareDatetime
    reference: str | None = None

    @model_validator(mode="after")
    def _window_ordered(self) -> Self:
        if self.window_end < self.window_start:
            raise ValueError("window_end must not be before window_start")
        return self


class Reconciliation(BaseModel):
    """One decision (one row of `reconciliations`)."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    expected_id: uuid.UUID | None
    delivery_id: uuid.UUID | None
    status: Status
    # 1.0 for a perfect pair, falling to 0.0 at the acceptance threshold. None when unpaired.
    score: float | None
    reason: str
    decided_at: datetime

    @staticmethod
    def make_id(expected_id: uuid.UUID | None, delivery_id: uuid.UUID | None) -> uuid.UUID:
        return uuid.uuid5(RECONCILIATION_NAMESPACE, f"{expected_id or '-'}|{delivery_id or '-'}")


class ReconcileResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    reconciliations: list[Reconciliation]
    # Expected deliveries with no match whose deadline (window_end + grace) has not passed yet.
    # They get no row; the next run (new delivery or timer) decides them.
    pending_expected_ids: list[uuid.UUID]
