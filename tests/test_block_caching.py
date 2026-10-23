"""Cache policy for upstream blocks.

A login wall or checkpoint page arrives as ``200 OK`` with no counters. Caching
that for the full profile TTL turns a temporary IP block into what looks like a
permanent outage, so blocked results must use the short negative TTL instead.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.config import Settings
from core.http import HttpClient
from models.schemas import (
    Confidence,
    Platform,
    ResultStatus,
    ScrapeResult,
    Source,
)
from platforms.base import AdapterContext
from platforms.instagram import InstagramAdapter


class RecordingCache:
    """Minimal cache that records the TTL of every write."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, int]] = []
        self._store: dict[str, Any] = {}

    async def get(self, key: str) -> Any | None:
        return self._store.get(key)

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        self.writes.append((key, ttl_seconds))
        self._store[key] = value

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def close(self) -> None:
        self._store.clear()

    def stats(self) -> dict[str, Any]:
        return {"entries": len(self._store)}


def _result(
    status: ResultStatus,
    *,
    code: str | None = None,
    followers: int | None = None,
) -> ScrapeResult:
    from models.schemas import Profile, ProfileMetrics

    return ScrapeResult(
        platform=Platform.instagram,
        kind="profile",
        url="https://www.instagram.com/nasa/",
        status=status,
        source=Source.html,
        confidence=Confidence.low if code else Confidence.medium,
        profile=Profile(
            platform=Platform.instagram,
            handle="nasa",
            url="https://www.instagram.com/nasa/",
            metrics=ProfileMetrics(followers=followers),
        ),
        error={"code": code, "message": "blocked"} if code else None,
    )


def _adapter(settings: Settings, http: HttpClient, cache: RecordingCache) -> InstagramAdapter:
    return InstagramAdapter(
        AdapterContext(settings=settings, http=http, cache=cache)  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("code", ["blocked", "auth_required", "rate_limited", "circuit_open"])
async def test_blocked_results_use_short_ttl(
    settings: Settings, http: HttpClient, code: str
) -> None:
    cache = RecordingCache()
    adapter = _adapter(settings, http, cache)

    async def factory() -> ScrapeResult:
        return _result(ResultStatus.partial, code=code)

    await adapter.cached("k", settings.cache_ttl_profile_seconds, factory)

    assert cache.writes == [("k", settings.cache_ttl_blocked_seconds)]


async def test_healthy_results_keep_full_ttl(
    settings: Settings, http: HttpClient
) -> None:
    cache = RecordingCache()
    adapter = _adapter(settings, http, cache)

    async def factory() -> ScrapeResult:
        return _result(ResultStatus.ok, followers=12345)

    await adapter.cached("k", settings.cache_ttl_profile_seconds, factory)

    assert cache.writes == [("k", settings.cache_ttl_profile_seconds)]


async def test_failed_results_are_never_cached(
    settings: Settings, http: HttpClient
) -> None:
    cache = RecordingCache()
    adapter = _adapter(settings, http, cache)

    async def factory() -> ScrapeResult:
        return _result(ResultStatus.failed, code="upstream_error")

    await adapter.cached("k", settings.cache_ttl_profile_seconds, factory)

    assert cache.writes == []


async def test_cached_blocked_result_is_served_back(
    settings: Settings, http: HttpClient
) -> None:
    cache = RecordingCache()
    adapter = _adapter(settings, http, cache)

    calls = 0

    async def factory() -> ScrapeResult:
        nonlocal calls
        calls += 1
        return _result(ResultStatus.partial, code="blocked")

    await adapter.cached("k", settings.cache_ttl_profile_seconds, factory)
    second = await adapter.cached("k", settings.cache_ttl_profile_seconds, factory)

    assert calls == 1
    assert second.cached is True
    assert second.error is not None
    assert second.error["code"] == "blocked"
