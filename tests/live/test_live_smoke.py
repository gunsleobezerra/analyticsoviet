"""Live network tests. Skipped unless ``LIVE=1`` is set in the environment.

These hit the real platforms, so they are opt-in and never run in CI by default::

    LIVE=1 .venv/bin/python -m pytest tests/live -q
"""

from __future__ import annotations

import pytest

from core.cache import MemoryCache
from core.config import Settings
from core.http import HttpClient
from models.schemas import Platform, ResultStatus
from platforms.base import AdapterContext
from platforms.registry import AdapterRegistry

pytestmark = pytest.mark.live

TARGETS: dict[Platform, str] = {
    Platform.youtube: "https://www.youtube.com/@mkbhd",
    Platform.tiktok: "https://www.tiktok.com/@khaby.lame",
    Platform.instagram: "https://www.instagram.com/nasa/",
    Platform.facebook: "https://www.facebook.com/nasa",
}


@pytest.fixture(scope="module")
async def registry() -> AdapterRegistry:
    settings = Settings(environment="test", enable_ytdlp_fallback=False)
    http = HttpClient(settings)
    await http.start()
    context = AdapterContext(settings=settings, http=http, cache=MemoryCache(max_entries=32))
    try:
        yield AdapterRegistry.default(context)
    finally:
        await http.aclose()


@pytest.mark.parametrize("platform", list(TARGETS))
async def test_profile_is_reachable_and_never_fails(registry: AdapterRegistry, platform: Platform) -> None:
    parsed = registry.detect(TARGETS[platform])
    adapter = registry.get(parsed.platform)
    result = await adapter.fetch_profile(parsed, include_posts=True, posts_limit=3, refresh=True)

    assert result.status != ResultStatus.failed
    assert result.platform == platform
    assert result.profile is not None or result.error.get("code") == "auth_required"


async def test_youtube_profile_yields_counters_and_posts(registry: AdapterRegistry) -> None:
    parsed = registry.detect(TARGETS[Platform.youtube])
    result = await registry.get(Platform.youtube).fetch_profile(
        parsed, include_posts=True, posts_limit=3, refresh=True
    )
    assert result.profile is not None
    assert (result.profile.metrics.followers or 0) > 1_000_000
    assert result.posts, "YouTube should expose recent uploads from embedded JSON"
    assert all(post.id for post in result.posts)


async def test_tiktok_profile_exposes_follower_counter(registry: AdapterRegistry) -> None:
    parsed = registry.detect(TARGETS[Platform.tiktok])
    result = await registry.get(Platform.tiktok).fetch_profile(
        parsed, include_posts=False, posts_limit=0, refresh=True
    )
    assert result.profile is not None
    assert (result.profile.metrics.followers or 0) > 1_000_000
    likes_total = result.profile.metrics.likes_total
    assert likes_total is None or likes_total > 0, "32-bit wraparound must be corrected"
