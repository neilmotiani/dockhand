"""Shared fixtures. Database tests run against a real Postgres (the queue relies on
`FOR UPDATE SKIP LOCKED` and the dedupe on unique constraints, which a fake would not prove).

Locally: `docker compose up -d db`. Without a reachable database these tests are skipped,
unless REQUIRE_DB=1 (set in CI), which turns the skip into a failure.
"""

import asyncio
import os

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.models import metadata
from app.db.session import make_engine

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://dockhand:dockhand@localhost:5432/dockhand_test"
)


async def _ensure_database(url: str) -> None:
    u = make_url(url)
    conn = await asyncpg.connect(
        user=u.username, password=u.password, host=u.host, port=u.port, database="postgres"
    )
    try:
        exists = await conn.fetchval("select 1 from pg_database where datname = $1", u.database)
        if not exists:
            await conn.execute(f'create database "{u.database}"')
    finally:
        await conn.close()


async def _reset_schema(url: str) -> None:
    engine = make_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.drop_all)
            await conn.run_sync(metadata.create_all)
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def database_url() -> str:
    try:
        asyncio.run(_ensure_database(TEST_DATABASE_URL))
        asyncio.run(_reset_schema(TEST_DATABASE_URL))
    except (OSError, asyncpg.PostgresError) as e:
        if os.environ.get("REQUIRE_DB") == "1":
            raise
        pytest.skip(f"Postgres unavailable at {TEST_DATABASE_URL}: {e}")
    return TEST_DATABASE_URL


@pytest.fixture
async def engine(database_url: str) -> AsyncEngine:
    engine = make_engine(database_url)
    tables = ", ".join(t.name for t in metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"truncate {tables} cascade"))
    yield engine
    await engine.dispose()
