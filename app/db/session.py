"""Engine creation and schema setup."""

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.db.models import metadata


def make_engine(database_url: str) -> AsyncEngine:
    # pool_pre_ping: the API outlives Postgres restarts during a demo without stale connections.
    return create_async_engine(database_url, pool_pre_ping=True, pool_size=10)


async def init_db(engine: AsyncEngine) -> None:
    """Create missing tables. `create_all` is enough while nothing is deployed; it never alters
    an existing table, so the first schema change after deploy is when migrations come in
    (docs/decisions.md)."""
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
