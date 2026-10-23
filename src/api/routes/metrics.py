"""Derived-metrics and history endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Query

from api.deps import StateDep
from api.urls import resolve_profile_url
from core.errors import UnsupportedUrlError
from db.snapshots import fetch_history
from models.schemas import HistoryResponse, Platform, ResultStatus, ScrapeResult
from services.normalize import build_derived

router = APIRouter(tags=["metrics"])


@router.get(
    "/metrics/{platform}/{handle}",
    response_model=ScrapeResult,
    summary="Profile metrics plus derived engagement indicators",
)
async def metrics(
    platform: Platform,
    handle: str,
    state: StateDep,
    posts_limit: int = Query(default=12, ge=0, le=50),
    refresh: bool = Query(default=False),
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
)
async def history(
    platform: Platform,
    handle: str,
    state: StateDep,
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=500, ge=1, le=5000),
) -> HistoryResponse:
    return await fetch_history(state.db, platform, handle, days=days, limit=limit)
