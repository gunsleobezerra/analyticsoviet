"""Derived-metrics and history endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query

from api.deps import StateDep
from api.urls import resolve_profile_url
from core.errors import UnsupportedUrlError
from db.snapshots import fetch_history
from models.schemas import ErrorResponse, HistoryResponse, Platform, ResultStatus, ScrapeResult
from services.normalize import build_derived

router = APIRouter(tags=["metrics"])

_PROFILE_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {
        "model": ErrorResponse,
        "description": "`unsupported_url` — the handle could not be resolved to a profile.",
    },
    401: {
        "model": ErrorResponse,
        "description": "`auth_required` — upstream needs a session; export a Netscape cookie jar.",
    },
    403: {
        "model": ErrorResponse,
        "description": "`blocked` — upstream rejected the request (bot check / login wall).",
    },
    404: {"model": ErrorResponse, "description": "`not_found` — the profile does not exist."},
    429: {"model": ErrorResponse, "description": "`rate_limited` — back off and retry."},
    502: {
        "model": ErrorResponse,
        "description": "`parse_error` / `upstream_error` — payload unreadable or upstream failed.",
    },
    503: {"model": ErrorResponse, "description": "`circuit_open` — the platform breaker tripped."},
}


@router.get(
    "/metrics/{platform}/{handle}",
    response_model=ScrapeResult,
    summary="Profile metrics plus derived engagement indicators",
    description=(
        "Same payload as `/v1/profile/{platform}/{handle}`, with `derived` filled in "
        "from the profile plus the sampled posts:\n\n"
        "| Field | Formula |\n"
        "|---|---|\n"
        "| `engagement_rate` | (likes + comments) of the best sampled post ÷ followers |\n"
        "| `avg_likes` / `avg_comments` / `avg_views` | mean across sampled posts |\n"
        "| `avg_engagement_rate` | mean per-post (likes + comments) ÷ followers |\n"
        "| `views_per_follower` | mean views ÷ followers |\n"
        "| `top_posts` | sampled posts ranked by engagement |\n\n"
        "Indicators are `null` when the underlying counters are unavailable — a missing "
        "denominator is never treated as zero."
    ),
    response_description="Profile payload with a populated `derived` block.",
    responses=_PROFILE_ERROR_RESPONSES,
)
async def metrics(
    platform: Annotated[Platform, Path(description="Platform that owns the profile.")],
    handle: Annotated[
        str,
        Path(
            description="Handle without `@`, or a full profile URL.",
            examples=["nasa"],
        ),
    ],
    state: StateDep,
    posts_limit: Annotated[
        int,
        Query(
            ge=0,
            le=50,
            description="How many posts to sample for the derived indicators (`0` disables them).",
        ),
    ] = 12,
    refresh: Annotated[
        bool, Query(description="Ignore the cache entry and hit upstream again.")
    ] = False,
) -> ScrapeResult:
    adapter = state.registry.get(platform)
    parsed = resolve_profile_url(adapter, platform, handle)
    if parsed is None:
        raise UnsupportedUrlError("could not build a profile URL", detail={"handle": handle})

    result = await adapter.fetch_profile(
        parsed,
        include_posts=posts_limit > 0,
        posts_limit=posts_limit,
        refresh=refresh,
    )
    if result.status != ResultStatus.failed:
        result.derived = result.derived or build_derived(result.profile, result.posts)
    return result


@router.get(
    "/history/{platform}/{handle}",
    response_model=HistoryResponse,
    summary="Metric history recorded by this service",
    description=(
        "Returns the snapshots persisted while history was enabled, oldest first, plus "
        "the delta between the first and the last point in the window.\n\n"
        "History is written by `/v1/profile`, `/v1/metrics` and `POST /v1/scrape` for every "
        "result whose status is not `failed`. Requires `ENABLE_HISTORY=true`; otherwise the "
        "list is empty. This endpoint never calls upstream."
    ),
    response_description="Recorded points for the window and their aggregate deltas.",
    responses={
        200: {
            "description": (
                "History window (possibly empty when history is disabled or the profile "
                "was never scraped)."
            )
        },
        422: {"description": "`days` or `limit` out of range."},
    },
)
async def history(
    platform: Annotated[Platform, Path(description="Platform that owns the profile.")],
    handle: Annotated[
        str,
        Path(
            description="Handle without `@`, or a full profile URL.",
            examples=["nasa"],
        ),
    ],
    state: StateDep,
    days: Annotated[
        int, Query(ge=1, le=365, description="How far back to look, in days.")
    ] = 30,
    limit: Annotated[
        int, Query(ge=1, le=5000, description="Maximum number of points returned (newest kept).")
    ] = 500,
) -> HistoryResponse:
    return await fetch_history(state.db, platform, handle, days=days, limit=limit)
