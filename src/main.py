"""FastAPI application entrypoint.

Everything long-lived (HTTP pool, cache, database, adapters) is created once in
the lifespan so requests only ever borrow shared, already-warm resources.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from api.deps import build_state
from api.routes import metrics, scrape, system
from core.cache import create_cache
from core.config import Settings, get_settings
from core.errors import ScrapeError
from core.http import HttpClient
from core.logging import bind_request_id, configure_logging, get_logger
from db.session import Database
from models.schemas import Platform

LOGGER = get_logger("app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_logging(settings.log_level, json_logs=settings.environment != "dev")

    http = HttpClient(settings)
    await http.start()

    cache = create_cache(settings)
    db = Database(settings)
    await db.start()

    app.state.app_state = build_state(settings, http, cache, db)
    LOGGER.info(
        "app_started",
        environment=settings.environment,
        version=settings.app_version,
        cache=settings.cache_backend,
        history=db.enabled,
    )
    try:
        yield
    finally:
        await http.aclose()
        await cache.close()
        await db.aclose()
        LOGGER.info("app_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title="analyticsoviet",
        description=(
            "Social media metrics API. Extracts public data from YouTube, TikTok, "
            "Instagram and Facebook using plain HTML/embedded JSON only — no headless "
            "browser is ever launched."
        ),
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.state.settings = settings

    @app.middleware("http")
    async def request_context_middleware(request: Request, call_next: Any) -> Any:
        request_id = bind_request_id(request.headers.get("x-request-id"))
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except ScrapeError as exc:  # pragma: no cover - handled by exception handler
            raise exc
        response.headers["x-request-id"] = request_id
        response.headers["x-response-time-ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
        LOGGER.info(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return response

    @app.exception_handler(ScrapeError)
    async def scrape_error_handler(_: Request, exc: ScrapeError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.to_dict()})

    app.include_router(system.router, prefix=settings.api_prefix)
    app.include_router(scrape.router, prefix=settings.api_prefix)
    app.include_router(metrics.router, prefix=settings.api_prefix)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, Any]:
        return {
            "name": settings.app_name,
            "version": settings.app_version,
            "docs": "/docs",
            "api": settings.api_prefix,
            "platforms": [platform.value for platform in Platform],
        }

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
