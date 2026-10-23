"""Typed errors shared by every adapter.

Every failure mode is represented here so routes can translate them into
consistent HTTP responses while still returning a partial payload.
"""

from __future__ import annotations

from typing import Any


class ScrapeError(Exception):
    """Base class for all scraper failures."""

    code: str = "scrape_error"
    http_status: int = 502
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        platform: str | None = None,
        url: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.platform = platform
        self.url = url
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.platform:
            payload["platform"] = self.platform
        if self.url:
            payload["url"] = self.url
        if self.detail:
            payload["detail"] = self.detail
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}({self.message!r}, platform={self.platform!r})"


class UnsupportedUrlError(ScrapeError):
    """The URL does not belong to any supported platform/resource shape."""

    code = "unsupported_url"
    http_status = 400


class NotFoundError(ScrapeError):
    """The profile or post does not exist (or was removed)."""

    code = "not_found"
    http_status = 404


class BlockedError(ScrapeError):
    """Upstream rejected the request (login wall, bot check, rate limit)."""

    code = "blocked"
    http_status = 403
    retryable = True


class AuthRequiredError(ScrapeError):
    """The resource needs an authenticated session (cookies) to be visible."""

    code = "auth_required"
    http_status = 401


class RateLimitedError(ScrapeError):
    """Local rate limiter or upstream 429."""

    code = "rate_limited"
    http_status = 429
    retryable = True


class ParseError(ScrapeError):
    """HTML was fetched but the expected payload could not be extracted."""

    code = "parse_error"
    http_status = 502


class UpstreamError(ScrapeError):
    """Network failure, timeout or 5xx from the upstream host."""

    code = "upstream_error"
    http_status = 502
    retryable = True


class CircuitOpenError(ScrapeError):
    """Circuit breaker is open for this platform; request rejected early."""

    code = "circuit_open"
    http_status = 503
    retryable = True
