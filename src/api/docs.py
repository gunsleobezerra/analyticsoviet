"""Swagger UI wiring and OpenAPI metadata.

The UI assets are served from the ``swagger-ui-bundle`` package instead of a
public CDN, so ``/docs`` keeps working on an air-gapped host or inside a
container without egress. If the package is missing the code falls back to the
jsDelivr CDN URLs FastAPI uses by default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.openapi.docs import get_swagger_ui_html, get_swagger_ui_oauth2_redirect_html
from fastapi.responses import FileResponse, HTMLResponse

from models.schemas import ErrorResponse

DOCS_ASSET_PREFIX = "/docs-assets"

# Only these files are exposed; the vendor directory also ships source maps.
_ASSET_MEDIA_TYPES: dict[str, str] = {
    "swagger-ui-bundle.js": "application/javascript",
    "swagger-ui.css": "text/css",
    "favicon-16x16.png": "image/png",
    "favicon-32x32.png": "image/png",
}

_CDN_BASE = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5"

API_DESCRIPTION = """
Public metrics for **YouTube**, **TikTok**, **Instagram** and **Facebook**, extracted from
plain HTML and embedded JSON. No headless browser is ever launched, so every payload declares
how it was obtained and how much to trust it.

### How to read a result

| Field | Meaning |
|---|---|
| `status` | `ok` (everything asked for was found), `partial` (some metrics missing), `failed` (nothing usable) |
| `source` | `html`, `embedded_json`, `oembed`, `ytdlp` or `official_api` — where the numbers came from |
| `confidence` | `high` / `medium` / `low` — reliability of that source for this platform |
| `warnings` | Human-readable list of metrics that could not be extracted |
| `cached` | `true` when served from the in-process/Redis cache without touching upstream |

A failure never aborts a batch: `POST /v1/scrape` answers `200` and marks only the failing
item as `failed`, leaving the rest of the results intact.

### Platform matrix

| Platform | Profile | Posts | Reliability | Primary source |
|---|---|---|---|---|
| YouTube | followers, posts | views, likes, comments | high | `ytInitialData` (+ `yt-dlp` / Data API v3 fallbacks) |
| TikTok | followers, following, likes, posts | views, likes, comments, shares | medium-high | `__UNIVERSAL_DATA…` (+ oEmbed) |
| Instagram | followers, following, posts | likes, comments | medium | Open Graph tags (+ `data-sjs` when present) |
| Facebook | followers/likes (partial) | reactions/comments (limited) | low | `mbasic.facebook.com` + Open Graph |

### Errors

Errors raised by a single scrape are mapped to a typed envelope:

```json
{"error": {"code": "blocked", "message": "upstream rejected the request", "platform": "instagram"}}
```

| HTTP | `code` | Retryable |
|---|---|---|
| 400 | `unsupported_url` | no |
| 401 | `auth_required` | no — export a Netscape cookie jar |
| 403 | `blocked` | yes — cookies or a residential proxy usually help |
| 404 | `not_found` | no |
| 429 | `rate_limited` | yes — back off |
| 502 | `parse_error` / `upstream_error` | sometimes |
| 503 | `circuit_open` | yes — the platform breaker tripped |

### Authentication

The API itself is unauthenticated. Upstream platforms are not: export Netscape cookie jars
(`cookies/<platform>.txt`) to raise coverage on Instagram, Facebook and TikTok.

### Rate limits and concurrency

Each platform has an independent local limiter and concurrency cap (`RATELIMIT_<PLATFORM>_PER_MINUTE`,
`CONCURRENCY_<PLATFORM>`). Exceeding them yields `429 rate_limited` without touching upstream, and
repeated upstream failures open a circuit breaker (`503 circuit_open`) for the cooldown window.

### Interactive tooling

* **Swagger UI** — [`/docs`](/docs), served from bundled assets (works offline / air-gapped)
* **ReDoc** — [`/redoc`](/redoc)
* **OpenAPI schema** — [`/openapi.json`](/openapi.json)
""".strip()

OPENAPI_TAGS: list[dict[str, str]] = [
    {
        "name": "system",
        "description": (
            "Liveness, diagnostics and the per-platform capability matrix. "
            "Use `/v1/health` for container healthchecks and `/v1/platforms` to discover "
            "which metrics each platform can actually deliver from HTML alone."
        ),
    },
    {
        "name": "scrape",
        "description": (
            "Fetch a profile or a single post. Results are cached and, when history is "
            "enabled, recorded as a snapshot for `/v1/history`."
        ),
    },
    {
        "name": "metrics",
        "description": (
            "Derived indicators (engagement rate, averages, top posts) computed from a "
            "profile plus a sample of its posts, and the recorded metric history."
        ),
    },
]


EXTERNAL_DOCS: dict[str, str] = {
    "description": "README — deployment, cookie jars and per-platform caveats.",
    "url": "https://github.com/leoso/analyticsoviet#readme",
}

def build_servers(public_base_url: str | None = None) -> list[dict[str, str]]:
    """Server list for the OpenAPI document.

    The relative entry keeps *Try it out* working behind a proxy or inside a
    container, where the browser origin already points at the right host.
    """
    servers = [{"url": "/", "description": "Current host (relative to the mount point)."}]
    if public_base_url:
        servers.insert(0, {"url": public_base_url.rstrip("/"), "description": "PUBLIC_BASE_URL."})
    servers.append({"url": "http://127.0.0.1:8000", "description": "Local development server."})
    return servers

def _error_response(status: int, code: str, description: str, example: dict[str, Any]) -> dict[str, Any]:
    """One error entry: schema plus a concrete, copy-pasteable example."""
    return {
        "model": ErrorResponse,
        "description": description,
        "content": {
            "application/json": {
                "examples": {code: {"summary": f"{status} {code}", "value": {"error": example}}}
            }
        },
    }

# Shared by every endpoint that can fail on a single resource, so the documented
# error envelope cannot drift between routers.
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: _error_response(
        400,
        "unsupported_url",
        "`unsupported_url` — the handle or URL could not be resolved to a profile/post.",
        {
            "code": "unsupported_url",
            "message": "URL does not match a supported platform",
            "url": "https://example.com/p/123",
        },
    ),
    401: _error_response(
        401,
        "auth_required",
        "`auth_required` — upstream needs a session; export a Netscape cookie jar.",
        {
            "code": "auth_required",
            "message": "upstream requires an authenticated session",
            "platform": "instagram",
            "url": "https://www.instagram.com/nasa/",
        },
    ),
    403: _error_response(
        403,
        "blocked",
        "`blocked` — upstream rejected the request (bot check / login wall). "
        "Cookies or a residential proxy usually help.",
        {
            "code": "blocked",
            "message": "upstream rejected the request",
            "platform": "facebook",
            "url": "https://www.facebook.com/nasa",
        },
    ),
    404: _error_response(
        404,
        "not_found",
        "`not_found` — the profile or post does not exist (or was removed).",
        {
            "code": "not_found",
            "message": "profile not found",
            "platform": "tiktok",
            "url": "https://www.tiktok.com/@this_handle_does_not_exist",
        },
    ),
    429: _error_response(
        429,
        "rate_limited",
        "`rate_limited` — local limiter or upstream 429. Retry after backing off.",
        {
            "code": "rate_limited",
            "message": "local rate limit exceeded, retry later",
            "platform": "youtube",
            "detail": {"retry_after_seconds": 12},
        },
    ),
    502: _error_response(
        502,
        "parse_error",
        "`parse_error` / `upstream_error` — payload unreadable or the upstream host failed.",
        {
            "code": "parse_error",
            "message": "could not locate ytInitialData in the payload",
            "platform": "youtube",
            "url": "https://www.youtube.com/@nasa",
        },
    ),
    503: _error_response(
        503,
        "circuit_open",
        "`circuit_open` — the platform breaker tripped; retry after the cooldown.",
        {
            "code": "circuit_open",
            "message": "circuit breaker open for instagram",
            "platform": "instagram",
            "detail": {"retry_after_seconds": 60},
        },
    ),
}

def _assets_dir() -> Path | None:
    """Return the vendored swagger-ui directory, or ``None`` when unavailable."""
    try:
        from swagger_ui_bundle import swagger_ui_path
    except ImportError:  # pragma: no cover - optional dependency
        return None
    path = Path(swagger_ui_path)
    return path if path.is_dir() else None


def install_docs(app: FastAPI, *, title: str) -> None:
    """Serve Swagger UI locally and wire the OAuth2 redirect helper."""
    assets = _assets_dir()

    if assets is not None:

        @app.get(
            f"{DOCS_ASSET_PREFIX}/{{filename}}",
            include_in_schema=False,
            summary="Swagger UI static asset",
        )
        async def docs_asset(filename: str) -> FileResponse:
            media_type = _ASSET_MEDIA_TYPES.get(filename)
            target = assets / filename
            if media_type is None or not target.is_file():
                raise HTTPException(status_code=404, detail="asset not found")
            return FileResponse(target, media_type=media_type)

        js_url = f"{DOCS_ASSET_PREFIX}/swagger-ui-bundle.js"
        css_url = f"{DOCS_ASSET_PREFIX}/swagger-ui.css"
        favicon_url = f"{DOCS_ASSET_PREFIX}/favicon-32x32.png"
    else:  # pragma: no cover - only when the bundle is not installed
        js_url = f"{_CDN_BASE}/swagger-ui-bundle.js"
        css_url = f"{_CDN_BASE}/swagger-ui.css"
        favicon_url = "https://fastapi.tiangolo.com/img/favicon.png"

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=app.openapi_url or "/openapi.json",
            title=f"{title} — API docs",
            oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
            swagger_js_url=js_url,
            swagger_css_url=css_url,
            swagger_favicon_url=favicon_url,
            swagger_ui_parameters={
                "docExpansion": "list",
                "defaultModelsExpandDepth": 1,
                "displayRequestDuration": True,
                "filter": True,
                "persistAuthorization": True,
                "tryItOutEnabled": True,
                "syntaxHighlight.theme": "monokai",
            },
        )

    @app.get(app.swagger_ui_oauth2_redirect_url or "/docs/oauth2-redirect", include_in_schema=False)
    async def swagger_ui_redirect() -> HTMLResponse:
        return get_swagger_ui_oauth2_redirect_html()
