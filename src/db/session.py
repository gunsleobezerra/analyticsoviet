"""Async database engine and session management."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.config import Settings
from core.logging import get_logger
from db.models import Base

LOGGER = get_logger("db")


def _apply_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """Set the per-connection pragmas every query relies on.

    ``foreign_keys`` is off by default in SQLite, which would make every FK in
    the schema decorative; ``busy_timeout`` keeps a concurrent writer waiting
    instead of failing with "database is locked"; ``synchronous=NORMAL`` is the
    right durability/speed trade under WAL.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def _enable_wal(path: Path) -> None:
    """Switch the database file to WAL once, outside any transaction.

    WAL is a property of the file, not of a connection, so one short-lived
    connection is enough for every later reader and writer.
    """
    connection = sqlite3.connect(path, isolation_level=None, timeout=5.0)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
    finally:
        connection.close()


class Database:
    """Owns the engine and hands out sessions.

    The schema is created by the migration runner before the process starts, not
    here. The one exception is the ``test`` environment, where the schema comes
    straight from the models so each test database is complete and disposable.

    The engine starts even when metric history is off: the persisted configuration
    lives in the same database and cannot depend on that switch.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None

    @property
    def enabled(self) -> bool:
        return self.settings.enable_history

    async def start(self) -> None:
        if self.engine is not None:
            return

        url = self.settings.database_url
        path = _sqlite_path(url) if url.startswith("sqlite") else None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            _enable_wal(path)

        self.engine = create_async_engine(url, echo=False, future=True)
        if url.startswith("sqlite"):
            event.listen(self.engine.sync_engine, "connect", _apply_sqlite_pragmas)
        self._sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

        if self.settings.environment == "test":
            async with self.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            LOGGER.info("db_ready", url=url, schema="models")
            return

        LOGGER.info("db_ready", url=url, schema_version=_schema_version(url))

    async def aclose(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None
            self._sessionmaker = None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        if self._sessionmaker is None:
            raise RuntimeError("database not started")
        async with self._sessionmaker() as session:
            yield session


def _schema_version(url: str) -> int | None:
    """Report the migrated schema version, so a missing migration shows up in the log."""
    from db import migrate

    try:
        return migrate.current_version(url)
    except Exception:  # pragma: no cover - diagnostics must never block the boot
        return None


def _sqlite_path(url: str) -> Path | None:
    prefix = "sqlite+aiosqlite:///"
    if not url.startswith(prefix):
        return None
    raw = url[len(prefix) :]
    if not raw or raw == ":memory:":
        return None
    return Path(raw)
