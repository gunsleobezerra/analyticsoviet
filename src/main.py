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
from api.docs import API_DESCRIPTION, OPENAPI_TAGS, install_docs
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

    if settings.cookies_storage_backend == "s3":
        try:
            from core.cookies_s3 import sync_cookies_from_s3

            downloaded = await sync_cookies_from_s3(settings)
            LOGGER.info("cookies_s3_synced", count=len(downloaded), files=downloaded)
        except Exception as exc:
            LOGGER.warning("cookies_s3_sync_failed", error=str(exc))

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
        description=API_DESCRIPTION,
        version=settings.app_version,
        summary="Public social media metrics from HTML — no headless browser.",
        lifespan=lifespan,
        docs_url=None,
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        openapi_tags=OPENAPI_TAGS,
        contact={
            "name": "analyticsoviet",
            "url": "https://github.com/leoso/analyticsoviet",
        },
        license_info={"name": "MIT"},
        swagger_ui_oauth2_redirect_url="/docs/oauth2-redirect",
    )
    app.state.settings = settings

    # Swagger UI served from the bundled assets (no CDN dependency).
    install_docs(app, title="analyticsoviet")

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

    @app.get(
        "/",
        tags=["system"],
        summary="Service banner",
        description="Name, version, documentation URL and the platforms enabled in this instance.",
        response_description="Service metadata.",
    )
    async def root() -> dict[str, Any]:
        return {
            "name": settings.app_name,
            "version": settings.app_version,
            "docs": "/docs",
            "redoc": "/redoc",
            "openapi": "/openapi.json",
            "api": settings.api_prefix,
            "platforms": [platform.value for platform in Platform],
        }

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
