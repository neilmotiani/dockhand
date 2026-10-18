"""Engine creation and schema setup."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.db.models import SOURCE_CHECK, metadata


def make_engine(database_url: str) -> AsyncEngine:
    # pool_pre_ping: the API outlives Postgres restarts during a demo without stale connections.
    return create_async_engine(database_url, pool_pre_ping=True, pool_size=10)


# Changes made after tables already existed in development databases. `create_all` never alters
# an existing table, so each change is replayed here as idempotent DDL (a no-op on a fresh
# schema). This stays a short list until the first deploy brings in real migrations
# (docs/decisions.md).
UPGRADES = (
    # Demo history: simulated orders, and `simulated` as a delivery source.
    "alter table expected_deliveries add column if not exists simulated boolean not null"
    " default false",
    "alter table deliveries drop constraint if exists deliveries_source",
    f"alter table deliveries add constraint deliveries_source check ({SOURCE_CHECK})",
)


async def init_db(engine: AsyncEngine) -> None:
    """Create missing tables, then apply UPGRADES."""
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        for stmt in UPGRADES:
            await conn.execute(text(stmt))
