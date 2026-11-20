"""Async database ulanishi."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.database.models import Base

logger = logging.getLogger(__name__)


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
    """Jadvallarni yaratadi va mavjud jadvallarga yangi ustunlarni qo'shadi.

    Oddiy avtomatik migratsiya: faqat ustun QO'SHADI (o'chirmaydi, turini o'zgartirmaydi),
    shuning uchun mavjud ma'lumotlar xavfsiz qoladi.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        added = await conn.run_sync(_add_missing_columns)
    if added:
        logger.info("db_columns_added %s", ", ".join(added))


def _add_missing_columns(conn) -> list[str]:
    insp = inspect(conn)
    preparer = conn.dialect.identifier_preparer
    added: list[str] = []
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            col_type = column.type.compile(dialect=conn.dialect)
            tbl, col = preparer.quote(table.name), preparer.quote(column.name)
            ddl = f"ALTER TABLE {tbl} ADD COLUMN {col} {col_type}"
            if column.server_default is not None:
                ddl += f" DEFAULT {column.server_default.arg.text}"
            conn.execute(text(ddl))
            added.append(f"{table.name}.{column.name}")
    return added
