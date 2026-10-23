"""Persistence and retrieval of metric snapshots."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from core.logging import get_logger
from db.models import ProfileSnapshot
from db.session import Database
from models.schemas import HistoryPoint, HistoryResponse, Platform, ScrapeResult

LOGGER = get_logger("db.snapshots")


async def record_result(db: Database, result: ScrapeResult) -> None:
    """Store a profile snapshot when the result carries usable metrics."""
    if not db.enabled or db.engine is None:
        return
    if result.profile is None or result.kind != "profile":
        return

    profile = result.profile
    metrics = profile.metrics
    if all(
        value is None
        for value in (metrics.followers, metrics.following, metrics.posts, metrics.likes_total)
    ):
        return

    snapshot = ProfileSnapshot(
        platform=result.platform.value,
        handle=profile.handle,
        followers=metrics.followers,
        following=metrics.following,
        posts=metrics.posts,
        likes_total=metrics.likes_total,
        engagement_rate=result.derived.engagement_rate if result.derived else None,
        source=result.source.value,
        confidence=result.confidence.value,
    )
    try:
        async with db.session() as session:
            session.add(snapshot)
            await session.commit()
    except Exception as exc:  # pragma: no cover - persistence must never break the API
        LOGGER.warning("snapshot_write_failed", platform=result.platform.value, error=str(exc))


async def fetch_history(
    db: Database,
    platform: Platform,
    handle: str,
    *,
    days: int = 30,
    limit: int = 500,
) -> HistoryResponse:
    points: list[HistoryPoint] = []

    if db.enabled and db.engine is not None:
        since = datetime.now(UTC) - timedelta(days=days)
        statement = (
            select(ProfileSnapshot)
            .where(
                ProfileSnapshot.platform == platform.value,
                ProfileSnapshot.handle == handle,
                ProfileSnapshot.captured_at >= since,
            )
            .order_by(ProfileSnapshot.captured_at.asc())
            .limit(limit)
        )
        try:
            async with db.session() as session:
                rows = (await session.scalars(statement)).all()
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("history_read_failed", platform=platform.value, error=str(exc))
            rows = []

        points = [
            HistoryPoint(
                captured_at=row.captured_at,
                followers=row.followers,
                following=row.following,
                posts=row.posts,
                likes_total=row.likes_total,
            )
            for row in rows
        ]

    delta_followers = None
    delta_posts = None
    if len(points) >= 2:
        if points[0].followers is not None and points[-1].followers is not None:
            delta_followers = points[-1].followers - points[0].followers
        if points[0].posts is not None and points[-1].posts is not None:
            delta_posts = points[-1].posts - points[0].posts

    return HistoryResponse(
        platform=platform,
        handle=handle,
        points=points,
        delta_followers=delta_followers,
        delta_posts=delta_posts,
    )
