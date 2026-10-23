"""Derived metrics and result post-processing shared by all platforms."""

from __future__ import annotations

from collections.abc import Iterable

from models.schemas import (
    Confidence,
    DerivedMetrics,
    Platform,
    Post,
    Profile,
    ResultStatus,
    ScrapeResult,
    Source,
)


def _mean(values: Iterable[int | float | None]) -> float | None:
    numbers = [float(value) for value in values if value is not None]
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers), 4)


def _post_engagement(post: Post) -> int | None:
    parts = [post.metrics.likes, post.metrics.comments, post.metrics.shares]
    known = [part for part in parts if part is not None]
    if not known:
        return None
    return sum(known)


def build_derived(profile: Profile | None, posts: list[Post]) -> DerivedMetrics:
    """Compute averages, engagement rate and the best-performing posts.

    Engagement rate uses the standard "interactions / followers" definition
    applied per post and then averaged, which avoids skew from a single viral
    post and from differing sample sizes.
    """
    followers = profile.metrics.followers if profile else None
    derived = DerivedMetrics(posts_sampled=len(posts))

    if not posts:
        return derived

    derived.avg_likes = _mean(post.metrics.likes for post in posts)
    derived.avg_comments = _mean(post.metrics.comments for post in posts)
    derived.avg_views = _mean(post.metrics.views for post in posts)

    if followers:
        rates: list[float] = []
        for post in posts:
            engagement = _post_engagement(post)
            if engagement is not None:
                rates.append(engagement / followers)
        if rates:
            derived.avg_engagement_rate = round(sum(rates) / len(rates), 6)

        total_views = [post.metrics.views for post in posts if post.metrics.views is not None]
        if total_views:
            derived.views_per_follower = round(
                (sum(total_views) / len(total_views)) / followers, 4
            )

    total_interactions = sum(
        engagement for post in posts if (engagement := _post_engagement(post)) is not None
    )
    if followers and total_interactions:
        derived.engagement_rate = round(total_interactions / followers / len(posts), 6)

    ranked = sorted(
        posts,
        key=lambda post: (
            (_post_engagement(post) or 0) + (post.metrics.views or 0) * 0.001,
            post.metrics.views or 0,
        ),
        reverse=True,
    )
    derived.top_posts = ranked[:3]
    return derived


def finalize(
    result: ScrapeResult,
    *,
    required: Iterable[str] = (),
    confidence: Confidence | None = None,
    source: Source | None = None,
) -> ScrapeResult:
    """Fill in derived data, status and confidence for a result."""
    if result.profile is not None or result.posts:
        result.derived = build_derived(result.profile, result.posts)

    if source is not None:
        result.source = source
    if confidence is not None:
        result.confidence = confidence

    missing = [field for field in required if not _has_field(result, field)]
    if missing:
        result.status = ResultStatus.partial
        for field in missing:
            result.add_warning(f"metric unavailable: {field}")

    if result.status != ResultStatus.failed and result.profile is None and not result.posts:
        result.status = ResultStatus.partial
    return result


def _has_field(result: ScrapeResult, field: str) -> bool:
    if field == "posts":
        # "posts" is satisfied either by a scraped sample or by the profile
        # counter; warning about it when the counter exists is misleading.
        if result.posts:
            return True
        return result.profile is not None and result.profile.metrics.posts is not None
    if field == "profile":
        return result.profile is not None
    if field == "post":
        return result.post is not None
    if result.profile is not None:
        value = getattr(result.profile.metrics, field, None)
        if value is not None:
            return True
    if result.post is not None:
        value = getattr(result.post.metrics, field, None)
        if value is not None:
            return True
    return False


def empty_result(
    *,
    platform: Platform,
    kind: str,
    url: str,
    source: Source = Source.html,
) -> ScrapeResult:
    return ScrapeResult(
        platform=platform,
        kind=kind,  # type: ignore[arg-type]
        url=url,
        status=ResultStatus.ok,
        source=source,
        confidence=Confidence.medium,
    )
