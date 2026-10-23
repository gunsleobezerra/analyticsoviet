"""Unified data contract returned by every adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class Platform(str, Enum):
    youtube = "youtube"
    tiktok = "tiktok"
    instagram = "instagram"
    facebook = "facebook"


class Source(str, Enum):
    html = "html"
    embedded_json = "embedded_json"
    official_api = "official_api"
    oembed = "oembed"
    ytdlp = "ytdlp"


class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class ResultStatus(str, Enum):
    ok = "ok"
    partial = "partial"
    failed = "failed"


class ProfileMetrics(BaseModel):
    """Audience-level numbers. ``None`` means "not available on this platform"."""

    followers: int | None = None
    following: int | None = None
    posts: int | None = None
    likes_total: int | None = None


class Profile(BaseModel):
    platform: Platform
    handle: str
    url: str
    display_name: str | None = None
    bio: str | None = None
    avatar_url: str | None = None
    verified: bool | None = None
    external_id: str | None = None
    metrics: ProfileMetrics = Field(default_factory=ProfileMetrics)
    extra: dict[str, Any] = Field(default_factory=dict)


class PostMetrics(BaseModel):
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    saves: int | None = None
    plays: int | None = None


class Post(BaseModel):
    platform: Platform
    id: str | None = None
    url: str
    caption: str | None = None
    thumbnail_url: str | None = None
    published_at: datetime | None = None
    duration_seconds: float | None = None
    media_type: Literal["video", "image", "short", "reel", "story", "unknown"] = "unknown"
    metrics: PostMetrics = Field(default_factory=PostMetrics)
    extra: dict[str, Any] = Field(default_factory=dict)


class DerivedMetrics(BaseModel):
    """Numbers computed from the profile plus a sample of posts."""

    posts_sampled: int = 0
    engagement_rate: float | None = None
    avg_likes: float | None = None
    avg_comments: float | None = None
    avg_views: float | None = None
    avg_engagement_rate: float | None = None
    views_per_follower: float | None = None
    top_posts: list[Post] = Field(default_factory=list)


class ScrapeResult(BaseModel):
    """Envelope for every scrape call.

    A result is only ``failed`` when nothing usable could be extracted. Partial
    data always comes back with ``status=partial`` plus ``warnings``.
    """

    platform: Platform
    kind: Literal["profile", "post"]
    url: str
    status: ResultStatus = ResultStatus.ok
    source: Source = Source.html
    confidence: Confidence = Confidence.medium
    fetched_at: datetime = Field(default_factory=utcnow)
    cached: bool = False
    profile: Profile | None = None
    posts: list[Post] = Field(default_factory=list)
    post: Post | None = None
    derived: DerivedMetrics | None = None
    warnings: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None

    def add_warning(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
            if self.status == ResultStatus.ok:
                self.status = ResultStatus.partial


class PlatformCapabilities(BaseModel):
    platform: Platform
    supports_profile: bool
    supports_posts: bool
    supports_single_post: bool
    html_reliability: Literal["high", "medium", "low"]
    profile_fields: list[str] = Field(default_factory=list)
    post_fields: list[str] = Field(default_factory=list)
    requires_cookies_for: list[str] = Field(default_factory=list)
    notes: str = ""


# --------------------------------------------------------------------------- API I/O


class ScrapeRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=50)
    include_posts: bool = True
    posts_limit: int = Field(default=12, ge=0, le=50)
    refresh: bool = False


class ScrapeResponse(BaseModel):
    results: list[ScrapeResult]
    elapsed_ms: float
    ok_count: int
    partial_count: int
    failed_count: int


class HistoryPoint(BaseModel):
    captured_at: datetime
    followers: int | None = None
    following: int | None = None
    posts: int | None = None
    likes_total: int | None = None


class HistoryResponse(BaseModel):
    platform: Platform
    handle: str
    points: list[HistoryPoint]
    delta_followers: int | None = None
    delta_posts: int | None = None


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str
    uptime_seconds: float
    cache: dict[str, Any] = Field(default_factory=dict)
    breakers: dict[str, str] = Field(default_factory=dict)
    platforms: list[str] = Field(default_factory=list)


class PlatformsResponse(BaseModel):
    platforms: list[PlatformCapabilities]
