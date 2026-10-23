"""Cache abstraction: in-process LRU-TTL by default, Redis when configured."""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from typing import Any, Protocol

from core.config import Settings
from core.logging import get_logger

LOGGER = get_logger("cache")


def cache_key(*parts: Any) -> str:
    return ":".join(str(part).strip().lower() for part in parts if part not in (None, ""))


class Cache(Protocol):
    async def get(self, key: str) -> Any | None: ...

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def close(self) -> None: ...

    def stats(self) -> dict[str, Any]: ...


class MemoryCache:
    """Ordered-dict LRU with per-entry TTL. Good enough for a single process."""

    def __init__(self, max_entries: int = 5000) -> None:
        self.max_entries = max(max_entries, 1)
        self._store: OrderedDict[str, tuple[float, str]] = OrderedDict()

    async def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if expires_at < time.monotonic():
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)
        try:
            return json.loads(payload)
        except json.JSONDecodeError:  # pragma: no cover - corrupt entry
            self._store.pop(key, None)
            return None

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        self._store[key] = (time.monotonic() + ttl_seconds, json.dumps(value, default=str))
        self._store.move_to_end(key)
        while len(self._store) > self.max_entries:
            self._store.popitem(last=False)

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def close(self) -> None:
        self._store.clear()

    def stats(self) -> dict[str, Any]:
        return {"entries": len(self._store), "max_entries": self.max_entries}


class RedisCache:
    """Redis-backed cache using ``redis.asyncio``."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._client: Any = None

    async def _conn(self) -> Any:
        if self._client is None:
            import redis.asyncio as redis  # imported lazily so redis stays optional

            self._client = redis.from_url(self._url, encoding="utf-8", decode_responses=True)
        return self._client

    async def get(self, key: str) -> Any | None:
        client = await self._conn()
        raw = await client.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:  # pragma: no cover
            return None

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        client = await self._conn()
        await client.set(key, json.dumps(value, default=str), ex=ttl_seconds)

    async def delete(self, key: str) -> None:
        client = await self._conn()
        await client.delete(key)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def stats(self) -> dict[str, Any]:
        return {"backend": "redis"}


def create_cache(settings: Settings) -> Cache:
    if settings.cache_backend == "redis":
        try:
            return RedisCache(settings.redis_url)
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("redis_init_failed", error=str(exc))
    return MemoryCache(max_entries=settings.cache_max_entries)
