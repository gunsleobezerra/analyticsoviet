"""Health and capability endpoints."""

from __future__ import annotations

import time

from fastapi import APIRouter

from api.deps import StateDep
from models.schemas import HealthResponse, PlatformsResponse

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse, summary="Liveness and diagnostics")
async def health(state: StateDep) -> HealthResponse:
    settings = state.settings
    breakers = {
        platform.value: state.http.breaker_state().get(platform.value, "closed")
        for platform in state.registry.platforms()
    }
    return HealthResponse(
        status="ok",
        version=settings.app_version,
        environment=settings.environment,
        uptime_seconds=round(time.monotonic() - state.started_at, 3),
        cache=state.cache.stats(),
        breakers=breakers,
        platforms=[platform.value for platform in state.registry.platforms()],
    )


@router.get(
    "/platforms",
    response_model=PlatformsResponse,
    summary="What each platform can deliver from HTML alone",
)
async def platforms(state: StateDep) -> PlatformsResponse:
    return PlatformsResponse(platforms=state.registry.capabilities())
