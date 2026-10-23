"""Per-platform token bucket + concurrency semaphore."""

from __future__ import annotations

import asyncio
import time

from core.config import Settings


class TokenBucket:
    """Simple async token bucket.

    ``rate`` is expressed in tokens per second, ``capacity`` is the burst size.
    """

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        self.rate = max(rate, 0.01)
        self.capacity = capacity if capacity is not None else max(self.rate, 1.0)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._updated
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait = deficit / self.rate
            await asyncio.sleep(min(wait, 5.0))


class PlatformLimiter:
    """Combines a token bucket and a semaphore per platform."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._buckets: dict[str, TokenBucket] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    def bucket(self, platform: str) -> TokenBucket:
        bucket = self._buckets.get(platform)
        if bucket is None:
            per_minute = max(self._settings.ratelimit_for(platform), 1)
            bucket = TokenBucket(rate=per_minute / 60.0, capacity=max(per_minute / 6.0, 1.0))
            self._buckets[platform] = bucket
        return bucket

    def semaphore(self, platform: str) -> asyncio.Semaphore:
        semaphore = self._semaphores.get(platform)
        if semaphore is None:
            semaphore = asyncio.Semaphore(max(self._settings.concurrency_for(platform), 1))
            self._semaphores[platform] = semaphore
        return semaphore

    async def acquire(self, platform: str) -> None:
        await self.bucket(platform).acquire()

    async def run_slot(self, platform: str) -> asyncio.Semaphore:
        """Return the semaphore so callers can ``async with`` it around I/O."""
        return self.semaphore(platform)
