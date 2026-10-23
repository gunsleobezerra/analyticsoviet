"""HTTP layer: user-agent rotation, cookie jars, retries, proxy, rate limiting.

All upstream traffic goes through :class:`HttpClient` so that politeness,
resilience and observability are enforced in a single place.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from core.breaker import CircuitBreaker
from core.config import Settings
from core.errors import (
    BlockedError,
    CircuitOpenError,
    NotFoundError,
    RateLimitedError,
    UpstreamError,
)
from core.logging import get_logger
from core.ratelimit import PlatformLimiter

LOGGER = get_logger("http")

BLOCK_MARKERS: tuple[str, ...] = (
    "please log in",
    "log in to continue",
    "you must log in",
    "login required",
    "entre para continuar",
    "faça login",
    "confirme que você não é um robô",
    "verify you are a human",
    "unusual traffic",
    "checkpoint",
    "captcha",
    "something went wrong",
    "conteúdo indisponível",
    "this content isn't available",
)

AUTH_MARKERS: tuple[str, ...] = (
    "login and password",
    "senha e login",
    "entrar no facebook",
    "log into facebook",
    "create new account",
    "crie uma conta",
)


@dataclass(slots=True)
class FetchResponse:
    """Minimal response wrapper handed to adapters."""

    url: str
    status: int
    text: str
    headers: Mapping[str, str] = field(default_factory=dict)
    final_url: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def looks_blocked(self) -> bool:
        lowered = self.text[:20000].lower()
        return any(marker in lowered for marker in BLOCK_MARKERS)

    def looks_auth_wall(self) -> bool:
        lowered = self.text[:20000].lower()
        return any(marker in lowered for marker in AUTH_MARKERS)


def parse_netscape_cookies(path: Path) -> list[dict[str, Any]]:
    """Parse a Netscape ``cookies.txt`` file into cookie dicts."""
    cookies: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, cookie_path, _secure, _expiry, name, value = parts[:7]
        cookies.append(
            {
                "domain": domain,
                "path": cookie_path or "/",
                "name": name,
                "value": value,
            }
        )
    return cookies


class HttpClient:
    """Shared async HTTP client with per-platform policy."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None
        self._limiter = PlatformLimiter(settings)
        self._breaker = CircuitBreaker(
            failure_threshold=settings.breaker_failure_threshold,
            cooldown_seconds=settings.breaker_cooldown_seconds,
            enabled=settings.breaker_enabled,
        )
        self._cookie_jars: dict[str, httpx.Cookies] = {}
        self._ua_by_platform: dict[str, str] = {}

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._client is not None:
            return
        timeout = httpx.Timeout(
            self.settings.http_timeout_seconds,
            connect=self.settings.http_connect_timeout_seconds,
        )
        limits = httpx.Limits(
            max_connections=self.settings.http_max_connections,
            max_keepalive_connections=self.settings.http_max_keepalive_connections,
        )
        self._client = httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            follow_redirects=True,
            http2=True,
            headers={
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                # NOTE: never hardcode Accept-Encoding. httpx negotiates it based on
                # the codecs actually installed (gzip/deflate/brotli/zstd). Sending
                # "br" without the brotli package yields an undecodable body.
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Upgrade-Insecure-Requests": "1",
            },
            proxy=self.settings.proxy_url or None,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ cookies
    def _cookies_for(self, platform: str) -> httpx.Cookies | None:
        if platform in self._cookie_jars:
            return self._cookie_jars[platform] or None

        jar = httpx.Cookies()
        path = self.settings.cookies_file_for(platform)
        if path is None:
            self._cookie_jars[platform] = jar
            return None
        try:
            parsed = parse_netscape_cookies(path)
        except OSError as exc:  # pragma: no cover - filesystem failure
            LOGGER.warning("cookies_read_failed", platform=platform, error=str(exc))
            self._cookie_jars[platform] = jar
            return None

        for cookie in parsed:
            jar.set(
                cookie["name"],
                cookie["value"],
                domain=cookie["domain"],
                path=cookie["path"],
            )
        self._cookie_jars[platform] = jar
        LOGGER.info("cookies_loaded", platform=platform, count=len(parsed))
        return jar if parsed else None

    def has_cookies(self, platform: str) -> bool:
        return self._cookies_for(platform) is not None

    def record_block(self, platform: str) -> None:
        """Count an application-level block against the platform breaker.

        A login wall or challenge page arrives as ``200 OK``, so the status
        based breaker never sees it. Adapters call this to make repeated soft
        blocks trip the breaker the same way a 429 does.
        """
        self._breaker.record_failure(platform)

    def user_agent(self, platform: str) -> str:
        ua = self._ua_by_platform.get(platform)
        if ua is None:
            ua = random.choice(self.settings.user_agents)
            self._ua_by_platform[platform] = ua
        return ua

    # ------------------------------------------------------------------ requests
    async def get(
        self,
        url: str,
        *,
        platform: str,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        retries: int | None = None,
    ) -> FetchResponse:
        """Perform a GET with retries, rate limiting and breaker protection."""
        if self._client is None:
            await self.start()
        assert self._client is not None

        if self.settings.breaker_enabled and not self._breaker.allow(platform):
            raise CircuitOpenError(
                f"circuit open for {platform}; retry later",
                platform=platform,
                url=url,
            )

        attempts = retries if retries is not None else self.settings.http_retries
        merged_headers: dict[str, str] = {
            "User-Agent": self.user_agent(platform),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if headers:
            merged_headers.update(headers)

        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            await self._limiter.acquire(platform)
            started = time.perf_counter()
            try:
                response = await self._client.get(
                    url,
                    headers=merged_headers,
                    params=dict(params) if params else None,
                    cookies=self._cookies_for(platform),
                )
            except httpx.TimeoutException:
                last_error = UpstreamError(
                    f"timeout after {self.settings.http_timeout_seconds}s",
                    platform=platform,
                    url=url,
                )
                LOGGER.warning(
                    "upstream_timeout", platform=platform, url=url, attempt=attempt
                )
                if attempt < attempts:
                    await self._sleep_backoff(attempt)
                continue
            except httpx.HTTPError as exc:
                last_error = UpstreamError(str(exc), platform=platform, url=url)
                LOGGER.warning(
                    "upstream_error", platform=platform, url=url, attempt=attempt, error=str(exc)
                )
                if attempt < attempts:
                    await self._sleep_backoff(attempt)
                continue

            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            LOGGER.info(
                "fetch",
                platform=platform,
                url=url,
                status=response.status_code,
                elapsed_ms=elapsed_ms,
                attempt=attempt,
            )

            if response.status_code == 404:
                self._breaker.record_success(platform)
                raise NotFoundError("resource not found", platform=platform, url=url)

            if response.status_code == 429:
                self._breaker.record_failure(platform)
                last_error = RateLimitedError(
                    "upstream rate limit", platform=platform, url=url
                )
                if attempt < attempts:
                    retry_after = _retry_after_seconds(response.headers)
                    await self._sleep_backoff(attempt, override=retry_after)
                    continue
                raise last_error

            if response.status_code in (401, 403):
                self._breaker.record_failure(platform)
                last_error = BlockedError(
                    f"upstream returned {response.status_code}",
                    platform=platform,
                    url=url,
                )
                if attempt < attempts:
                    await self._sleep_backoff(attempt)
                    continue
                raise last_error

            if response.status_code >= 500:
                self._breaker.record_failure(platform)
                last_error = UpstreamError(
                    f"upstream returned {response.status_code}",
                    platform=platform,
                    url=url,
                )
                if attempt < attempts:
                    await self._sleep_backoff(attempt)
                    continue
                raise last_error

            self._breaker.record_success(platform)
            return FetchResponse(
                url=url,
                status=response.status_code,
                text=response.text,
                headers=dict(response.headers),
                final_url=str(response.url),
            )

        raise last_error or UpstreamError("request failed", platform=platform, url=url)

    async def _sleep_backoff(self, attempt: int, *, override: float | None = None) -> None:
        if override is not None:
            delay = min(override, 10.0)
        else:
            base = self.settings.http_retry_backoff_seconds
            delay = min(base * (2 ** (attempt - 1)) + random.uniform(0, 0.3), 8.0)
        await asyncio.sleep(delay)

    # ------------------------------------------------------------------ introspection
    def breaker_state(self) -> dict[str, str]:
        return self._breaker.snapshot()


def _retry_after_seconds(headers: Mapping[str, str]) -> float:
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return 2.0
    try:
        return float(raw)
    except ValueError:
        return 2.0
