"""Async database engine and session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

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


class Database:
    """Thin wrapper that owns the engine and creates tables on startup."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None

    @property
    def enabled(self) -> bool:
        return self.settings.enable_history

    async def start(self) -> None:
        if not self.enabled or self.engine is not None:
            return

        url = self.settings.database_url
        if url.startswith("sqlite"):
            path = _sqlite_path(url)
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)

        self.engine = create_async_engine(url, echo=False, future=True)
        self._sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        LOGGER.info("db_ready", url=url)

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


def _sqlite_path(url: str) -> Path | None:
    prefix = "sqlite+aiosqlite:///"
    if not url.startswith(prefix):
        return None
    raw = url[len(prefix) :]
    if not raw or raw == ":memory:":
        return None
    return Path(raw)
