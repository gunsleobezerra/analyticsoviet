"""Shared pytest fixtures.

Network access is disabled by default: adapters get an ``HttpClient`` whose
transport raises, so any accidental outbound call fails loudly instead of
silently hitting the real platform. Tests that need HTML read the gzipped
fixtures in ``tests/fixtures``.
"""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import Any

import httpx
import pytest

from core.cache import MemoryCache
from core.config import Settings
from core.http import HttpClient
from platforms.base import AdapterContext
from platforms.registry import AdapterRegistry

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    """Read ``tests/fixtures/<name>``, transparently gunzipping ``.gz``."""
    path = FIXTURES / name
    if not path.exists():
        raise FileNotFoundError(f"missing fixture: {path}")
    if path.suffix == ".gz":
        return gzip.decompress(path.read_bytes()).decode("utf-8", errors="replace")
    return path.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(environment="test", enable_ytdlp_fallback=False, database_url="sqlite+aiosqlite:///:memory:")


class OfflineTransport(httpx.AsyncBaseTransport):
    """Fails every request so tests can never reach the network."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raise httpx.ConnectError("network disabled in tests", request=request)


@pytest.fixture
def offline_transport() -> OfflineTransport:
    return OfflineTransport()


@pytest.fixture
def http(settings: Settings, offline_transport: OfflineTransport) -> HttpClient:
    client = HttpClient(settings)
    client._client = httpx.AsyncClient(
        transport=offline_transport,
        timeout=settings.http_timeout_seconds,
    )
    return client


@pytest.fixture
def ctx(settings: Settings, http: HttpClient) -> AdapterContext:
    return AdapterContext(settings=settings, http=http, cache=MemoryCache(max_entries=8))


@pytest.fixture
def registry(ctx: AdapterContext) -> AdapterRegistry:
    return AdapterRegistry.default(ctx)


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - used by pytest-asyncio fallback
    return "asyncio"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``live`` tests unless ``LIVE=1`` is set."""
    import os

    if os.environ.get("LIVE") == "1":
        return
    skip_live = pytest.mark.skip(reason="live network test; set LIVE=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture
def fixture_html() -> Any:
    return load_fixture
