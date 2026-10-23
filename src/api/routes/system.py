"""Health and capability endpoints."""

from __future__ import annotations

import time

from fastapi import APIRouter

from api.deps import StateDep
from models.schemas import HealthResponse, PlatformsResponse

router = APIRouter(tags=["system"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness and diagnostics",
    description=(
        "Cheap, dependency-free probe used by container healthchecks and dashboards. "
        "It never calls upstream, so it stays fast even when every platform is blocked.\n\n"
        "`breakers` exposes the circuit-breaker state per platform: `closed` (healthy), "
        "`open` (requests are being rejected early) or `half_open` (probing recovery)."
    ),
    response_description="Process status, uptime, cache stats and breaker states.",
)
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
    description=(
        "Machine-readable capability matrix. Use it to decide, per platform, which metrics "
        "are reachable without a browser, which ones usually require an authenticated cookie "
        "jar (`requires_cookies_for`), and how stable the HTML extraction is "
        "(`html_reliability`).\n\n"
        "Absent metrics come back as `null` with a matching entry in `warnings` — treat "
        "`null` as \"unknown\", never as zero."
    ),
    response_description="One capability entry per enabled platform.",
)
async def platforms(state: StateDep) -> PlatformsResponse:
    return PlatformsResponse(platforms=state.registry.capabilities())
