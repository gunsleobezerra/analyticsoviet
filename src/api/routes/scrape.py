"""Scraping endpoints."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Query

from api.deps import AppState, StateDep
from api.urls import resolve_profile_url
from core.errors import ScrapeError, UnsupportedUrlError
from core.logging import get_logger
from db.snapshots import record_result
from models.schemas import (
    Platform,
    ResultStatus,
    ScrapeRequest,
    ScrapeResponse,
    ScrapeResult,
)
from platforms.base import ParsedUrl

router = APIRouter(tags=["scrape"])
LOGGER = get_logger("api.scrape")


@router.post(
    "/scrape",
    response_model=ScrapeResponse,
    summary="Scrape one or more profile/post URLs in parallel",
)
async def scrape(payload: ScrapeRequest, state: StateDep) -> ScrapeResponse:
    started = time.perf_counter()
    results = await asyncio.gather(
        *(_dispatch_url(state, url, payload) for url in payload.urls)
    )

    for result in results:
        if result.status != ResultStatus.failed:
            await record_result(state.db, result)

    return ScrapeResponse(
        results=list(results),
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        ok_count=sum(1 for r in results if r.status == ResultStatus.ok),
        partial_count=sum(1 for r in results if r.status == ResultStatus.partial),
        failed_count=sum(1 for r in results if r.status == ResultStatus.failed),
    )


@router.get(
    "/profile/{platform}/{handle}",
    response_model=ScrapeResult,
    summary="Scrape a single profile by platform and handle",
)
async def profile(
    platform: Platform,
    handle: str,
    state: StateDep,
    include_posts: bool = Query(default=True),
    posts_limit: int = Query(default=12, ge=0, le=50),
    refresh: bool = Query(default=False),
) -> ScrapeResult:
    adapter = state.registry.get(platform)
    parsed = resolve_profile_url(adapter, platform, handle)
    if parsed is None:
        raise UnsupportedUrlError("could not build a profile URL", detail={"handle": handle})

    result = await adapter.fetch_profile(
        parsed,
        include_posts=include_posts,
        posts_limit=posts_limit,
        refresh=refresh,
    )
    await record_result(state.db, result)
    return result


@router.get(
    "/post",
    response_model=ScrapeResult,
    summary="Scrape a single post/video by URL",
)
async def post(url: str, state: StateDep, refresh: bool = Query(default=False)) -> ScrapeResult:
    parsed = state.registry.detect(url)
    if parsed.kind != "post":
        raise UnsupportedUrlError("URL does not point to a post", detail={"url": url})
    adapter = state.registry.get(parsed.platform)
    return await adapter.fetch_post(parsed, refresh=refresh)


# ---------------------------------------------------------------------- helpers


async def _dispatch_url(state: AppState, url: str, payload: ScrapeRequest) -> ScrapeResult:
    parsed = state.registry.detect_or_none(url)
    if parsed is None:
        return ScrapeResult(
            platform=Platform.youtube,
            kind="profile",
            url=url,
            status=ResultStatus.failed,
            error={
                "code": UnsupportedUrlError.code,
                "message": "URL does not match a supported platform",
                "detail": url,
            },
        )
    return await _dispatch(state, parsed, payload)


async def _dispatch(
    state: AppState, parsed: ParsedUrl, payload: ScrapeRequest
) -> ScrapeResult:
    adapter = state.registry.get(parsed.platform)
    try:
        if parsed.kind == "post":
            return await adapter.fetch_post(parsed, refresh=payload.refresh)
        return await adapter.fetch_profile(
            parsed,
            include_posts=payload.include_posts,
            posts_limit=payload.posts_limit,
            refresh=payload.refresh,
        )
    except ScrapeError as exc:
        LOGGER.warning(
            "scrape_error",
            platform=parsed.platform.value,
            url=parsed.original,
            code=exc.code,
            error=exc.message,
        )
        return ScrapeResult(
            platform=parsed.platform,
            kind=parsed.kind,
            url=parsed.original,
            status=ResultStatus.failed,
            error=exc.to_dict(),
        )
