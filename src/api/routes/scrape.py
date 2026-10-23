"""Scraping endpoints."""

from __future__ import annotations

import asyncio
import time
from typing import Annotated

from fastapi import APIRouter, Path, Query

from api.deps import AppState, StateDep
from api.docs import ERROR_RESPONSES
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
    description=(
        "Dispatches every URL in the batch concurrently and returns one result per item, "
        "in the same order as the request.\n\n"
        "* A failing item **never** aborts the batch: the response is still `200` and only "
        "that item carries `status: \"failed\"` plus an `error` object.\n"
        "* Mixed batches (profiles and posts, multiple platforms) are supported.\n"
        "* Successful items are cached and, when history is enabled, persisted as snapshots "
        "so `/v1/history` can chart them later.\n\n"
        "Maximum of 50 URLs per request; each profile samples at most 50 posts."
    ),
    response_description="Per-item results plus aggregate counters and elapsed time.",
    responses={
        200: {
            "description": (
                "Batch processed. Inspect `ok_count`, `partial_count` and `failed_count` "
                "to know how many items actually succeeded."
            )
        },
        422: {"description": "Request body failed validation (empty `urls`, out-of-range `posts_limit`)."},
    },
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
    description=(
        "Accepts either a bare handle (`nasa`, `@mkbhd`) or a full profile URL and returns "
        "the identity plus audience counters, optionally with a sample of recent posts.\n\n"
        "The response declares `status`, `source` and `confidence`, so callers can tell "
        "extracted numbers from missing ones — absent metrics are `null` and listed in "
        "`warnings`, never silently zeroed."
    ),
    response_description="Profile payload (and sampled posts) with provenance metadata.",
    responses=ERROR_RESPONSES,
)
async def profile(
    platform: Annotated[Platform, Path(description="Platform that owns the profile.")],
    handle: Annotated[
        str,
        Path(
            description="Handle without `@`, or a full profile URL.",
            examples=["nasa"],
        ),
    ],
    state: StateDep,
    include_posts: Annotated[
        bool, Query(description="Attach the sampled post list to the profile result.")
    ] = True,
    posts_limit: Annotated[
        int,
        Query(
            ge=0,
            le=50,
            description="How many recent posts to sample (`0` disables post fetching).",
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
    description=(
        "Resolves the URL to its platform and fetches that single post/video. "
        "Supported shapes: `youtube.com/watch?v=…`, `youtu.be/…`, `youtube.com/shorts/…`, "
        "`tiktok.com/@user/video/…`, `instagram.com/p|reel|tv/…`, `facebook.com/…/posts/…`.\n\n"
        "Use `POST /v1/scrape` instead when you need several items in one round trip."
    ),
    response_description="The post with its counters and provenance metadata.",
    responses=ERROR_RESPONSES,
)
async def post(
    state: StateDep,
    url: Annotated[
        str,
        Query(
            description="Full post/video URL on a supported platform.",
            examples=["https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
        ),
    ],
    refresh: Annotated[
        bool, Query(description="Ignore the cache entry and hit upstream again.")
    ] = False,
) -> ScrapeResult:
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
