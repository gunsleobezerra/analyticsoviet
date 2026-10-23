"""Adapter contract implemented by each platform."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from core.cache import Cache
from core.config import Settings
from core.http import HttpClient
from core.logging import get_logger
from models.schemas import (
    Platform,
    PlatformCapabilities,
    ResultStatus,
    ScrapeResult,
)

UrlKind = Literal["profile", "post"]

_HANDLE_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")

# Error codes that mean "upstream gated us", not "the data does not exist".
# Caching those for the full profile TTL makes an outage look permanent.
_BLOCKING_ERROR_CODES = frozenset(
    {"auth_required", "blocked", "rate_limited", "circuit_open"}
)


@dataclass(frozen=True, slots=True)
class ParsedUrl:
    """Result of resolving a user-supplied URL."""

    platform: Platform
    kind: UrlKind
    original: str
    canonical_url: str
    handle: str | None = None
    resource_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AdapterContext:
    """Shared dependencies handed to every adapter."""

    settings: Settings
    http: HttpClient
    cache: Cache
    logger: Any = field(default_factory=lambda: get_logger("adapter"))


class BaseAdapter(ABC):
    """Every platform adapter exposes the same three operations."""

    platform: Platform
    capabilities: PlatformCapabilities

    def __init__(self, ctx: AdapterContext) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self.http = ctx.http
        self.cache = ctx.cache
        self.log = ctx.logger

    # ------------------------------------------------------------------ parsing
    @abstractmethod
    def detect(self, url: str) -> ParsedUrl | None:
        """Return a ParsedUrl when ``url`` belongs to this platform."""

    @abstractmethod
    async def fetch_profile(
        self,
        parsed: ParsedUrl,
        *,
        include_posts: bool = True,
        posts_limit: int = 12,
        refresh: bool = False,
    ) -> ScrapeResult:
        """Collect profile metrics (and optionally the latest posts)."""

    @abstractmethod
    async def fetch_post(self, parsed: ParsedUrl, *, refresh: bool = False) -> ScrapeResult:
        """Collect metrics for a single post/video."""

    # ------------------------------------------------------------------ helpers
    def cache_key(self, parsed: ParsedUrl, suffix: str = "") -> str:
        identifier = parsed.handle or parsed.resource_id or "unknown"
        parts = ["scrape", self.platform.value, parsed.kind, identifier]
        if suffix:
            parts.append(suffix)
        return ":".join(parts)

    async def cached(
        self,
        key: str,
        ttl_seconds: int,
        factory: Callable[[], Awaitable[ScrapeResult]],
        *,
        refresh: bool = False,
    ) -> ScrapeResult:
        """Cache-aside around an adapter call. Failed results are never cached."""
        if not refresh:
            hit = await self.cache.get(key)
            if hit is not None:
                try:
                    result = ScrapeResult.model_validate(hit)
                except Exception:  # pragma: no cover - stale/incompatible entry
                    result = None
                if result is not None:
                    result.cached = True
                    return result

        result = await factory()
        if result.status == ResultStatus.failed:
            return result
        ttl = ttl_seconds
        error = result.error
        if isinstance(error, dict) and error.get("code") in _BLOCKING_ERROR_CODES:
            ttl = min(ttl_seconds, self.settings.cache_ttl_blocked_seconds)
        await self.cache.set(key, result.model_dump(mode="json"), ttl)
        return result

    @staticmethod
    def clean_handle(raw: str | None) -> str | None:
        if not raw:
            return None
        handle = raw.strip().strip("/")
        if handle.startswith("@"):
            handle = handle[1:]
        if not handle or not _HANDLE_RE.match(handle):
            return None
        return handle

    @staticmethod
    def query_param(url: str, name: str) -> str | None:
        from urllib.parse import parse_qs, urlparse

        values = parse_qs(urlparse(url).query).get(name)
        return values[0] if values else None

    @staticmethod
    def strip_query(url: str) -> str:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
