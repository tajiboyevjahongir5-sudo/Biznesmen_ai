"""Async database ulanishi."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.database.models import Base


def create_engine(database_url: str) -> AsyncEngine:
    if database_url.startswith("sqlite"):
        # ./data/... papkasini yaratib qo'yamiz
        path = database_url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_async_engine(database_url, connect_args={"timeout": 30})

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record) -> None:  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

        return engine
    return create_async_engine(database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Jadvallarni yaratadi (MVP). Alembic migratsiyalari keyingi bosqichda ulanadi."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
