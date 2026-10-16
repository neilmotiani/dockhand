"""Event History polling fallback.

Ring documents webhooks only as going to a URL registered on an app during onboarding, and
we found no way to give the Developer Playground one (docs/decisions.md). The poller turns new
Event History entries into the same queue rows a webhook would create, deduped on Ring's
event ID.

Only `motion.human` and `ding` are requested by default. `on_demand` is excluded on purpose:
Event History defines it as "User or partner initiated a live view session", so our own WHEP
capture would appear there and polling it would trigger another capture.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from app.ingest.events import PayloadError, from_history, implied_sub_types, is_relevant
from app.ingest.queue import insert_event, utcnow
from app.ring.client import RingClient
from app.ring.devices import Device

log = logging.getLogger(__name__)

DEVICE_CACHE_TTL = timedelta(minutes=5)


class HistoryPoller:
    def __init__(
        self,
        ring: RingClient,
        engine: AsyncEngine,
        *,
        event_types: str,
        relevant_rules: list[str],
        lookback: timedelta,
        on_insert: Callable[[], None] = lambda: None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._ring = ring
        self._engine = engine
        self._event_types = event_types
        self._sub_types = implied_sub_types(event_types)
        self._rules = relevant_rules
        self._on_insert = on_insert
        self._clock = clock
        # Ignore history from before startup (minus a lookback): those events are too old to
        # capture live, and enqueuing the backlog would only produce skipped rows.
        self._since = clock() - lookback
        self._devices: list[Device] = []
        self._devices_at: datetime | None = None
        self._warned_filter = False

    async def _cameras(self) -> list[Device]:
        now = self._clock()
        if self._devices_at is None or now - self._devices_at > DEVICE_CACHE_TTL:
            self._devices = [d for d in await self._ring.list_devices() if d.is_camera]
            self._devices_at = now
        return self._devices

    def _page_sub_types(self, items: list[dict]) -> dict[str, str]:
        """The subtype implied by the filter, only if this page shows the filter was applied.

        On 2026-10-16 the Playground ignored `event_types` entirely (`motion.human,ding`, and
        even `bogus_type`, returned the same on_demand items; tests/fixtures/ring/
        history_events_*.json). If the filter is ignored, a returned motion item could be any
        subtype, so labelling it `human` would be invented data. A page containing a type we
        did not ask for proves the filter was not applied.
        """
        requested = {part.strip().partition(".")[0] for part in self._event_types.split(",")}
        returned = {(i.get("attributes") or {}).get("event_type") for i in items}
        if returned <= requested:
            return self._sub_types
        if not self._warned_filter:
            log.warning(
                "Event History ignored event_types=%s (returned %s); not inferring subtypes",
                self._event_types,
                sorted(t for t in returned if t),
            )
            self._warned_filter = True
        return {}

    async def poll_once(self) -> int:
        """Fetch history for every camera and enqueue new relevant events. Returns rows
        inserted. Irrelevant items (e.g. on_demand from our own live views) are not stored:
        unlike a webhook, nothing was pushed to us, and storing them would only add noise."""
        inserted = 0
        for device in await self._cameras():
            items = await self._ring.history_events(device.id, self._event_types)
            sub_types = self._page_sub_types(items)
            for item in items:
                try:
                    event = from_history(item, sub_types)
                except PayloadError:
                    log.warning("skipping malformed history item: %r", item)
                    continue
                if event.occurred_at is not None and event.occurred_at < self._since:
                    continue
                if not is_relevant(event, self._rules):
                    continue
                now = self._clock()
                async with self._engine.begin() as conn:
                    new_id = await insert_event(conn, event, True, received_at=now)
                if new_id is not None:
                    inserted += 1
        if inserted:
            self._on_insert()
        return inserted

    async def run(self, interval_s: float) -> None:
        while True:
            try:
                n = await self.poll_once()
                if n:
                    log.info("poller enqueued %d new events", n)
            except Exception:
                # Network error or an expired Playground token: log and keep polling. Once a
                # Playground token expires every poll fails until a new one is configured.
                log.exception("Event History poll failed")
            await asyncio.sleep(interval_s)
