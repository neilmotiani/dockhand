import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from app.assistant.api import router as assistant_router
from app.config import Settings, get_settings
from app.db.session import init_db, make_engine
from app.ingest.api import router as ingest_router
from app.ingest.poller import HistoryPoller
from app.ingest.queue import EventQueue, Processor, utcnow
from app.ledger.api import router as ledger_router
from app.reconcile.api import router as expected_router
from app.reconcile.cost import ReconcileConfig
from app.reconcile.store import ReconcileTimer
from app.ring.client import RingClient
from app.ring.tokens import PlaygroundTokenProvider

log = logging.getLogger(__name__)


def _default_pipeline(app: FastAPI, settings: Settings) -> Processor:
    # Imported here so the API (and its tests) start without boto3/aiortc being exercised.
    from app.capture.service import Capturer
    from app.extract.client import BedrockExtractor, make_bedrock_client
    from app.pipeline import Pipeline

    capturer = Capturer(app.state.ring, settings, clock=utcnow)
    extractor = BedrockExtractor(make_bedrock_client(settings), settings)
    return Pipeline(app.state.engine, settings, capturer, extractor, clock=utcnow)


def create_app(
    settings: Settings | None = None,
    *,
    engine: AsyncEngine | None = None,
    processor: Processor | None = None,
    start_background: bool = True,
    token_env_file: Path | None = Path(".env"),
    assistant: Any = None,
) -> FastAPI:
    """Build the API. Without a `processor`, events run through the real pipeline (WHEP
    capture + Bedrock). Tests pass their own engine and processor and turn off the background
    workers, poller and timer so they can drive the queue step by step."""
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
        # Re-read from .env on every call, so a fresh Playground token needs no restart.
        tokens = PlaygroundTokenProvider(settings, env_file=token_env_file)
        app.state.ring = RingClient(http, tokens)
        tasks: list[asyncio.Task] = []
        if start_background:
            proc = processor or _default_pipeline(app, settings)
            tasks += [
                asyncio.create_task(app.state.queue.worker(proc))
                for _ in range(settings.queue_workers)
            ]
            timer = ReconcileTimer(
                app.state.engine,
                ZoneInfo(settings.business_timezone),
                ReconcileConfig.from_settings(settings),
                clock=utcnow,
            )
            tasks.append(asyncio.create_task(timer.run(settings.reconcile_interval_s)))
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
    # Built on first use (app/assistant/api.py) unless a test passes one in.
    app.state.assistant = assistant
    app.include_router(ingest_router)
    app.include_router(expected_router)
    app.include_router(ledger_router)
    app.include_router(assistant_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
