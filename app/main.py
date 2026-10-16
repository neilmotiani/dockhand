import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings, get_settings
from app.db.session import init_db, make_engine
from app.ingest.api import router as ingest_router
from app.ingest.poller import HistoryPoller
from app.ingest.queue import EventQueue, Processor
from app.ring.client import RingClient
from app.ring.tokens import PlaygroundTokenProvider

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    engine: AsyncEngine | None = None,
    processor: Processor | None = None,
    start_background: bool = True,
) -> FastAPI:
    """Build the API. Tests pass their own engine and processor and turn off the background
    workers and poller so they can drive the queue step by step."""
    settings = settings or get_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        own_engine = engine is None
        app.state.engine = engine or make_engine(settings.database_url)
        await init_db(app.state.engine)
        app.state.queue = EventQueue(
            app.state.engine,
            max_attempts=settings.queue_max_attempts,
            lease_s=settings.queue_lease_s,
            idle_poll_s=settings.queue_idle_poll_s,
        )
        http = httpx.AsyncClient(
            base_url=settings.ring_api_base_url, timeout=settings.ring_http_timeout_s
        )
        app.state.ring = RingClient(http, PlaygroundTokenProvider(settings))
        tasks: list[asyncio.Task] = []
        if start_background:
            if processor is not None:
                tasks += [
                    asyncio.create_task(app.state.queue.worker(processor))
                    for _ in range(settings.queue_workers)
                ]
            if settings.ring_poll_interval_s > 0:
                poller = HistoryPoller(
                    app.state.ring,
                    app.state.engine,
                    event_types=settings.ring_poll_event_types,
                    relevant_rules=settings.ingest_relevant_events,
                    lookback=timedelta(seconds=settings.capture_max_event_age_s),
                    on_insert=app.state.queue.notify,
                )
                tasks.append(asyncio.create_task(poller.run(settings.ring_poll_interval_s)))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await http.aclose()
            if own_engine:
                await app.state.engine.dispose()

    app = FastAPI(title="Dockhand", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(ingest_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
