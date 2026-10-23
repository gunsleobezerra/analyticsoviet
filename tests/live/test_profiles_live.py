"""Live checks for the curated profile list in ``tests/test_profiles.txt``.

These hit the real platforms, so they are opt-in::

    LIVE=1 .venv/bin/python -m pytest tests/live/test_profiles_live.py -q

The assertions are deliberately tolerant: anonymous scraping legitimately
returns ``partial`` results (login walls, rate limits). A test only fails when
the adapter raises, reports ``failed``, or returns data that contradicts the
URL it was given.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from core.cache import MemoryCache
from core.config import Settings
from core.http import HttpClient
from models.schemas import Platform, ResultStatus, ScrapeResult
from platforms.base import AdapterContext
from platforms.registry import AdapterRegistry
from profile_cases import ProfileCase, load_cases

pytestmark = [pytest.mark.live, pytest.mark.asyncio(loop_scope="module")]

CASES = load_cases()
IDS = [case.id for case in CASES]

POSTS_LIMIT = 3


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def registry() -> AdapterRegistry:
    # Anonymous Instagram returns 401 on its private endpoint; that is an
    # expected auth wall, not an outage, so keep the breaker from opening and
    # cascading across the remaining profiles.
    settings = Settings(
        environment="test",
        enable_ytdlp_fallback=False,
        breaker_failure_threshold=1000,
    )
    http = HttpClient(settings)
    await http.start()
    context = AdapterContext(settings=settings, http=http, cache=MemoryCache(max_entries=64))
    try:
        yield AdapterRegistry.default(context)
    finally:
        await http.aclose()


async def _fetch(registry: AdapterRegistry, case: ProfileCase) -> ScrapeResult:
    parsed = registry.detect(case.url)
    assert parsed.kind == "profile", f"{case.url} detected as {parsed.kind}"
    return await registry.get(parsed.platform).fetch_profile(
        parsed, include_posts=True, posts_limit=POSTS_LIMIT, refresh=True
    )

def _error_code(result: ScrapeResult) -> str | None:
    error = result.error
    if isinstance(error, dict):
        return error.get("code")
    return getattr(error, "code", None)


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_profile_url_never_fails(registry: AdapterRegistry, case: ProfileCase) -> None:
    result = await _fetch(registry, case)

    assert result.platform == Platform(case.platform)
    assert result.kind == "profile"
    assert result.status != ResultStatus.failed, result.error
    if result.status == ResultStatus.partial:
        assert result.warnings or result.error, "partial result must explain itself"


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_profile_identity_matches_url(registry: AdapterRegistry, case: ProfileCase) -> None:
    result = await _fetch(registry, case)
    if result.profile is None:
        pytest.skip(f"no profile payload ({result.error or result.warnings})")

    assert result.profile.platform == Platform(case.platform)
    assert result.profile.handle.lower() == case.handle.lower()
    assert result.profile.url.startswith("http")


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_profile_metrics_are_sane(registry: AdapterRegistry, case: ProfileCase) -> None:
    result = await _fetch(registry, case)
    if result.profile is None:
        pytest.skip(f"no profile payload ({result.error or result.warnings})")
    if result.status == ResultStatus.partial and _error_code(result) == "auth_required":
        pytest.skip(f"platform requires an authenticated session ({case.url})")
    if result.status == ResultStatus.partial and any("login wall" in w or "unavailable" in w for w in result.warnings):
        pytest.skip(f"platform served partial response without metrics: {result.warnings} ({case.url})")

    metrics = result.profile.metrics
    for name in ("followers", "following", "posts", "likes_total"):
        value = getattr(metrics, name)
        assert value is None or value >= 0, f"{name}={value} must be a non-negative count"

    assert any(
        getattr(metrics, name) is not None
        for name in ("followers", "following", "posts", "likes_total")
    ), f"no headline metric extracted for {case.url}"


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_posts_are_well_formed(registry: AdapterRegistry, case: ProfileCase) -> None:
    result = await _fetch(registry, case)
    assert len(result.posts) <= POSTS_LIMIT

    now = datetime.now(UTC)
    for post in result.posts:
        assert post.platform == Platform(case.platform)
        assert post.id, f"post without id in {case.url}"
        assert post.url.startswith("http")
        assert post.duration_seconds is None or post.duration_seconds >= 0
        if post.published_at is not None:
            published = post.published_at
            if published.tzinfo is None:
                published = published.replace(tzinfo=UTC)
            assert published <= now, f"post published in the future: {post.published_at}"
        for name in ("views", "likes", "comments", "shares", "plays"):
            value = getattr(post.metrics, name)
            assert value is None or value >= 0


async def test_youtube_channel_exposes_counters(registry: AdapterRegistry) -> None:
    youtube = [case for case in CASES if case.platform == "youtube"]
    if not youtube:
        pytest.skip("no YouTube profile in the list")

    for case in youtube:
        result = await _fetch(registry, case)
        if result.profile is None:
            pytest.skip(f"no profile payload for {case.url} ({result.error})")
        assert (result.profile.metrics.followers or 0) > 0, case.url
        assert (result.profile.metrics.posts or 0) > 0, case.url


async def test_list_has_at_least_one_working_profile_per_platform(
    registry: AdapterRegistry,
) -> None:
    per_platform: dict[str, int] = {}
    for case in CASES:
        result = await _fetch(registry, case)
        if result.profile is not None:
            per_platform[case.platform] = per_platform.get(case.platform, 0) + 1

    expected = {case.platform for case in CASES}
    broken = sorted(expected - set(per_platform))
    assert not broken, f"no usable profile payload for: {broken}"
