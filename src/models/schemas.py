"""Unified data contract returned by every adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class Platform(str, Enum):
    """Supported upstream platforms."""

    youtube = "youtube"
    tiktok = "tiktok"
    instagram = "instagram"
    facebook = "facebook"


class Source(str, Enum):
    """Where the extracted numbers came from."""

    html = "html"
    embedded_json = "embedded_json"
    official_api = "official_api"
    oembed = "oembed"
    ytdlp = "ytdlp"


class Confidence(str, Enum):
    """How much the reported numbers can be trusted for this platform."""

    high = "high"
    medium = "medium"
    low = "low"


class ResultStatus(str, Enum):
    """Outcome of a scrape."""

    ok = "ok"
    partial = "partial"
    failed = "failed"


class ProfileMetrics(BaseModel):
    """Audience-level numbers. ``None`` means "not available on this platform"."""

    followers: int | None = Field(
        default=None, description="Follower (or subscriber) count.", examples=[97000000]
    )
    following: int | None = Field(
        default=None, description="Number of accounts this profile follows.", examples=[280]
    )
    posts: int | None = Field(
        default=None, description="Total number of published posts/videos.", examples=[4300]
    )
    likes_total: int | None = Field(
        default=None,
        description="Lifetime likes/hearts, only exposed by TikTok.",
        examples=[1200000000],
    )


class Profile(BaseModel):
    """Public identity of an account plus its audience-level counters."""

    platform: Platform = Field(description="Platform that produced this profile.")
    handle: str = Field(description="Canonical handle, without the leading ``@``.", examples=["nasa"])
    url: str = Field(description="Canonical profile URL.", examples=["https://www.instagram.com/nasa/"])
    display_name: str | None = Field(default=None, description="Display name as shown upstream.")
    bio: str | None = Field(default=None, description="Profile bio/description, when published.")
    avatar_url: str | None = Field(default=None, description="Absolute URL of the avatar image.")
    verified: bool | None = Field(
        default=None, description="Whether the platform marks the account as verified."
    )
    external_id: str | None = Field(
        default=None, description="Platform-internal account id, when present in the payload."
    )
    metrics: ProfileMetrics = Field(
        default_factory=ProfileMetrics, description="Audience-level counters."
    )
    extra: dict[str, Any] = Field(
        default_factory=dict, description="Platform-specific fields that have no canonical slot."
    )


class PostMetrics(BaseModel):
    """Per-post counters. Only the metrics the platform actually exposes are filled."""

    views: int | None = Field(default=None, description="View/play count.", examples=[2500000])
    likes: int | None = Field(default=None, description="Like/heart count.", examples=[12000])
    comments: int | None = Field(default=None, description="Comment count.", examples=[340])
    shares: int | None = Field(default=None, description="Share count (TikTok only).", examples=[900])
    saves: int | None = Field(default=None, description="Save/bookmark count, when exposed.")
    plays: int | None = Field(default=None, description="Play count when distinct from ``views``.")


class Post(BaseModel):
    """A single post, video or reel."""

    platform: Platform = Field(description="Platform that produced this post.")
    id: str | None = Field(default=None, description="Platform-native post id.")
    url: str = Field(description="Canonical post URL.")
    caption: str | None = Field(default=None, description="Caption or video title.")
    thumbnail_url: str | None = Field(default=None, description="Absolute thumbnail image URL.")
    published_at: datetime | None = Field(default=None, description="Publication timestamp (UTC).")
    duration_seconds: float | None = Field(default=None, description="Media duration in seconds.")
    media_type: Literal["video", "image", "short", "reel", "story", "unknown"] = Field(
        default="unknown", description="Shape of the media attached to the post."
    )
    metrics: PostMetrics = Field(default_factory=PostMetrics, description="Per-post counters.")
    extra: dict[str, Any] = Field(
        default_factory=dict, description="Platform-specific fields that have no canonical slot."
    )


class DerivedMetrics(BaseModel):
    """Numbers computed from the profile plus a sample of posts."""

    posts_sampled: int = Field(default=0, description="How many posts were used in the sample.")
    engagement_rate: float | None = Field(
        default=None,
        description="(likes + comments) of the best sampled post divided by followers.",
        examples=[0.0042],
    )
    avg_likes: float | None = Field(default=None, description="Mean likes across sampled posts.")
    avg_comments: float | None = Field(default=None, description="Mean comments across sampled posts.")
    avg_views: float | None = Field(default=None, description="Mean views across sampled posts.")
    avg_engagement_rate: float | None = Field(
        default=None, description="Mean per-post engagement divided by followers."
    )
    views_per_follower: float | None = Field(
        default=None, description="Mean views divided by followers."
    )
    top_posts: list[Post] = Field(
        default_factory=list, description="Sampled posts ranked by engagement."
    )


class ScrapeResult(BaseModel):
    """Envelope for every scrape call.

    A result is only ``failed`` when nothing usable could be extracted. Partial
    data always comes back with ``status=partial`` plus ``warnings``.
    """

    platform: Platform = Field(description="Platform the URL resolved to.")
    kind: Literal["profile", "post"] = Field(description="Whether a profile or a single post was scraped.")
    url: str = Field(description="URL that was scraped (canonicalised when possible).")
    status: ResultStatus = Field(
        default=ResultStatus.ok, description="`ok`, `partial` or `failed`."
    )
    source: Source = Field(default=Source.html, description="Where the data came from.")
    confidence: Confidence = Field(default=Confidence.medium, description="Reliability of that source.")
    fetched_at: datetime = Field(default_factory=utcnow, description="When the upstream payload was fetched (UTC).")
    cached: bool = Field(default=False, description="True when served from cache without hitting upstream.")
    profile: Profile | None = Field(default=None, description="Profile payload for `kind=profile`.")
    posts: list[Post] = Field(default_factory=list, description="Sampled posts for `kind=profile`.")
    post: Post | None = Field(default=None, description="Post payload for `kind=post`.")
    derived: DerivedMetrics | None = Field(
        default=None, description="Derived indicators, present when posts were sampled."
    )
    warnings: list[str] = Field(
        default_factory=list, description="Metrics that could not be extracted, in human-readable form."
    )
    error: dict[str, Any] | None = Field(
        default=None, description="Error envelope when `status=failed`."
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "platform": "instagram",
                "kind": "profile",
                "url": "https://www.instagram.com/nasa/",
                "status": "partial",
                "source": "html",
                "confidence": "medium",
                "fetched_at": "2026-10-23T00:00:00Z",
                "cached": False,
                "profile": {
                    "platform": "instagram",
                    "handle": "nasa",
                    "url": "https://www.instagram.com/nasa/",
                    "display_name": "NASA",
                    "metrics": {"followers": 97000000, "posts": 4300},
                },
                "posts": [
                    {
                        "platform": "instagram",
                        "url": "https://www.instagram.com/p/x/",
                        "metrics": {"likes": 12000},
                    }
                ],
                "derived": {"posts_sampled": 1, "engagement_rate": 0.0001, "top_posts": []},
                "warnings": ["metric unavailable: following"],
                "error": None,
            }
        }
    }

    def add_warning(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
            if self.status == ResultStatus.ok:
                self.status = ResultStatus.partial


class PlatformCapabilities(BaseModel):
    """What a given platform can deliver from HTML alone."""

    platform: Platform = Field(description="Platform these capabilities describe.")
    supports_profile: bool = Field(description="Whether profiles can be scraped.")
    supports_posts: bool = Field(description="Whether a post list can be attached to a profile.")
    supports_single_post: bool = Field(description="Whether an individual post can be fetched by URL.")
    html_reliability: Literal["high", "medium", "low"] = Field(
        description="How stable the HTML extraction is for this platform."
    )
    profile_fields: list[str] = Field(default_factory=list, description="Profile metrics that are reachable.")
    post_fields: list[str] = Field(default_factory=list, description="Post metrics that are reachable.")
    requires_cookies_for: list[str] = Field(
        default_factory=list, description="Fields that usually need an authenticated cookie jar."
    )
    notes: str = Field(default="", description="Free-form caveats about this platform.")


# --------------------------------------------------------------------------- API I/O


class ScrapeRequest(BaseModel):
    """Batch scrape request."""

    urls: list[str] = Field(
        min_length=1,
        max_length=50,
        description="Profile or post URLs to scrape. Each is dispatched in parallel.",
        examples=[["https://www.instagram.com/nasa/", "https://www.tiktok.com/@nasa"]],
    )
    include_posts: bool = Field(
        default=True, description="Attach the sampled post list to profile results."
    )
    posts_limit: int = Field(
        default=12, ge=0, le=50, description="How many posts to sample per profile (0 disables)."
    )
    refresh: bool = Field(default=False, description="Bypass the cache and hit upstream again.")

    model_config = {
        "json_schema_extra": {
            "example": {
                "urls": [
                    "https://www.instagram.com/nasa/",
                    "https://www.tiktok.com/@nasa",
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                ],
                "include_posts": True,
                "posts_limit": 6,
                "refresh": False,
            }
        }
    }


class ScrapeResponse(BaseModel):
    """Batch result. Failures are per item and never abort the batch."""

    results: list[ScrapeResult] = Field(description="One entry per requested URL, in input order.")
    elapsed_ms: float = Field(description="Total wall-clock time of the batch, in milliseconds.")
    ok_count: int = Field(description="Results with `status=ok`.")
    partial_count: int = Field(description="Results with `status=partial`.")
    failed_count: int = Field(description="Results with `status=failed`.")

    model_config = {
        "json_schema_extra": {
            "example": {
                "results": [
                    {
                        "platform": "tiktok",
                        "kind": "profile",
                        "url": "https://www.tiktok.com/@nasa",
                        "status": "ok",
                        "source": "embedded_json",
                        "confidence": "medium",
                        "fetched_at": "2026-10-23T00:00:00Z",
                        "cached": False,
                        "profile": {
                            "platform": "tiktok",
                            "handle": "nasa",
                            "url": "https://www.tiktok.com/@nasa",
                            "metrics": {"followers": 6100000, "following": 12, "likes_total": 48000000},
                        },
                        "posts": [],
                        "derived": None,
                        "warnings": [],
                        "error": None,
                    },
                    {
                        "platform": "instagram",
                        "kind": "profile",
                        "url": "https://www.instagram.com/nasa/",
                        "status": "failed",
                        "source": "html",
                        "confidence": "low",
                        "fetched_at": "2026-10-23T00:00:00Z",
                        "cached": False,
                        "profile": None,
                        "posts": [],
                        "derived": None,
                        "warnings": [],
                        "error": {
                            "code": "blocked",
                            "message": "upstream rejected the request",
                            "platform": "instagram",
                        },
                    },
                ],
                "elapsed_ms": 1842.6,
                "ok_count": 1,
                "partial_count": 0,
                "failed_count": 1,
            }
        }
    }


class HistoryPoint(BaseModel):
    """One recorded snapshot of a profile's counters."""

    captured_at: datetime = Field(description="When the snapshot was taken (UTC).")
    followers: int | None = Field(default=None, description="Followers at that time.")
    following: int | None = Field(default=None, description="Following at that time.")
    posts: int | None = Field(default=None, description="Post count at that time.")
    likes_total: int | None = Field(default=None, description="Lifetime likes at that time.")


class HistoryResponse(BaseModel):
    """Recorded metric history for one profile."""

    platform: Platform = Field(description="Platform of the profile.")
    handle: str = Field(description="Handle of the profile.")
    points: list[HistoryPoint] = Field(description="Snapshots ordered oldest to newest.")
    delta_followers: int | None = Field(
        default=None, description="Difference in followers between the first and last point."
    )
    delta_posts: int | None = Field(
        default=None, description="Difference in post count between the first and last point."
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "platform": "instagram",
                "handle": "nasa",
                "points": [
                    {
                        "captured_at": "2026-09-23T00:00:00Z",
                        "followers": 96800000,
                        "following": 280,
                        "posts": 4280,
                        "likes_total": None,
                    },
                    {
                        "captured_at": "2026-10-23T00:00:00Z",
                        "followers": 97000000,
                        "following": 280,
                        "posts": 4300,
                        "likes_total": None,
                    },
                ],
                "delta_followers": 200000,
                "delta_posts": 20,
            }
        }
    }


class HealthResponse(BaseModel):
    """Liveness and diagnostics."""

    status: str = Field(description="Always `ok` when the process is serving.", examples=["ok"])
    version: str = Field(description="Application version.", examples=["0.1.0"])
    environment: str = Field(description="Deployment environment.", examples=["prod"])
    uptime_seconds: float = Field(description="Seconds since the process started.")
    cache: dict[str, Any] = Field(default_factory=dict, description="Cache backend and hit counters.")
    breakers: dict[str, str] = Field(
        default_factory=dict, description="Circuit-breaker state per platform (`closed`/`open`/`half_open`)."
    )
    platforms: list[str] = Field(default_factory=list, description="Platforms enabled in this instance.")

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "ok",
                "version": "0.1.0",
                "environment": "prod",
                "uptime_seconds": 812.431,
                "cache": {"backend": "redis", "hits": 1284, "misses": 96, "entries": 412},
                "breakers": {
                    "youtube": "closed",
                    "tiktok": "closed",
                    "instagram": "half_open",
                    "facebook": "open",
                },
                "platforms": ["youtube", "tiktok", "instagram", "facebook"],
            }
        }
    }


class PlatformsResponse(BaseModel):
    """Capability matrix for every enabled platform."""

    platforms: list[PlatformCapabilities] = Field(description="One entry per platform.")

    model_config = {
        "json_schema_extra": {
            "example": {
                "platforms": [
                    {
                        "platform": "youtube",
                        "supports_profile": True,
                        "supports_posts": True,
                        "supports_single_post": True,
                        "html_reliability": "high",
                        "profile_fields": ["followers", "posts"],
                        "post_fields": ["views", "likes", "comments"],
                        "requires_cookies_for": [],
                        "notes": "ytInitialData is stable; yt-dlp is an optional fallback.",
                    }
                ]
            }
        }
    }


class ErrorPayload(BaseModel):
    """Typed error body returned for scrape failures."""

    code: str = Field(description="Stable machine-readable error code.", examples=["blocked"])
    message: str = Field(description="Human-readable explanation.")
    platform: str | None = Field(default=None, description="Platform involved, when known.")
    url: str | None = Field(default=None, description="URL involved, when known.")
    detail: dict[str, Any] | None = Field(default=None, description="Extra context for debugging.")


class ErrorResponse(BaseModel):
    """Envelope wrapping every non-2xx answer from the API."""

    error: ErrorPayload = Field(description="The error details.")

    model_config = {
        "json_schema_extra": {
            "example": {
                "error": {
                    "code": "blocked",
                    "message": "upstream rejected the request",
                    "platform": "instagram",
                    "url": "https://www.instagram.com/nasa/",
                    "detail": {"status_code": 403},
                }
            }
        }
    }


class ServiceInfo(BaseModel):
    """Service banner returned by ``GET /``."""

    name: str = Field(description="Application name.", examples=["analyticsoviet"])
    version: str = Field(description="Application version.", examples=["0.1.0"])
    docs: str = Field(description="URL of the Swagger UI.", examples=["/docs"])
    redoc: str = Field(description="URL of the ReDoc page.", examples=["/redoc"])
    openapi: str = Field(description="URL of the OpenAPI document.", examples=["/openapi.json"])
    api: str = Field(description="Prefix shared by every versioned endpoint.", examples=["/v1"])
    platforms: list[str] = Field(description="Platforms enabled in this instance.")

    model_config = {
        "json_schema_extra": {
            "example": {
                "name": "analyticsoviet",
                "version": "0.1.0",
                "docs": "/docs",
                "redoc": "/redoc",
                "openapi": "/openapi.json",
                "api": "/v1",
                "platforms": ["youtube", "tiktok", "instagram", "facebook"],
            }
        }
    }
