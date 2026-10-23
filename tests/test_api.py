"""HTTP-level tests for the FastAPI surface.

Every adapter network call is stubbed, so these tests exercise routing,
serialisation and error mapping only -- never the network.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

import platforms.facebook as fb
import platforms.instagram as ig
import platforms.tiktok as tt
import platforms.youtube as yt
from core.config import Settings
from core.errors import UnsupportedUrlError
from main import create_app
from models.schemas import (
    Platform,
    Post,
    PostMetrics,
    Profile,
    ProfileMetrics,
    ResultStatus,
    ScrapeResult,
    Source,
)


def _profile_result(platform: Platform, handle: str) -> ScrapeResult:
    return ScrapeResult(
        platform=platform,
        kind="profile",
        url=f"https://example.com/{handle}",
        status=ResultStatus.ok,
        source=Source.html,
        profile=Profile(
            platform=platform,
            handle=handle,
            url=f"https://example.com/{handle}",
            display_name="Example",
            metrics=ProfileMetrics(followers=1_000, posts=10),
        ),
        posts=[
            Post(
                platform=platform,
                id="p1",
                url=f"https://example.com/{handle}/p1",
                metrics=PostMetrics(likes=100, comments=10, views=5_000),
            )
        ],
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    settings = Settings(
        environment="test",
        enable_ytdlp_fallback=False,
        database_url="sqlite+aiosqlite:///:memory:",
    )
    app = create_app(settings)

    async def fake_profile(self: object, parsed: object, **_kwargs: object) -> ScrapeResult:
        platform = self.platform
        return _profile_result(platform, getattr(parsed, "handle", "x"))

    async def fake_post(self: object, parsed: object, **_kwargs: object) -> ScrapeResult:
        platform = self.platform
        return ScrapeResult(
            platform=platform,
            kind="post",
            url="https://example.com/p",
            status=ResultStatus.ok,
            source=Source.html,
            post=Post(platform=platform, id="abc", url="https://example.com/p"),
        )

    for adapter_cls in (yt.YouTubeAdapter, tt.TikTokAdapter, ig.InstagramAdapter, fb.FacebookAdapter):
        monkeypatch.setattr(adapter_cls, "fetch_profile", fake_profile)
        monkeypatch.setattr(adapter_cls, "fetch_post", fake_post)

    with TestClient(app) as test_client:
        state = app.state.app_state
        # Belt and braces: no request may escape to the network.
        transport = httpx.MockTransport(lambda request: httpx.Response(599, request=request))
        state.http._client = httpx.AsyncClient(transport=transport)
        yield test_client


def test_health(client: TestClient) -> None:
    response = client.get("/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["environment"] == "test"
    assert body["platforms"] == ["facebook", "instagram", "tiktok", "youtube"]


def test_root_advertises_platforms(client: TestClient) -> None:
    body = client.get("/").json()
    assert body["api"] == "/v1"
    assert set(body["platforms"]) == {"youtube", "tiktok", "instagram", "facebook"}


def test_platforms_endpoint_reports_capabilities(client: TestClient) -> None:
    body = client.get("/v1/platforms").json()
    by_name = {entry["platform"]: entry for entry in body["platforms"]}
    assert by_name["youtube"]["html_reliability"] == "high"
    assert by_name["facebook"]["html_reliability"] == "low"


def test_response_carries_request_id_and_timing(client: TestClient) -> None:
    response = client.get("/v1/health", headers={"x-request-id": "abc-123"})
    assert response.headers["x-request-id"] == "abc-123"
    assert float(response.headers["x-response-time-ms"]) >= 0.0


def test_scrape_rejects_unsupported_url(client: TestClient) -> None:
    response = client.post("/v1/scrape", json={"urls": ["https://example.com/nope"]})
    assert response.status_code == 200
    body = response.json()
    assert body["failed_count"] == 1
    assert body["results"][0]["error"]["code"] == UnsupportedUrlError.code


def test_scrape_unknown_platform_returns_400(client: TestClient) -> None:
    response = client.get("/v1/profile/myspace/someone")
    assert response.status_code == 422


def test_profile_endpoint_returns_a_scrape_result(client: TestClient) -> None:
    response = client.get("/v1/profile/youtube/mkbhd?posts_limit=1")
    assert response.status_code == 200
    body = response.json()
    assert body["platform"] == "youtube"
    assert body["profile"]["handle"] == "mkbhd"
    assert body["status"] == "ok"


def test_post_endpoint_rejects_a_profile_url(client: TestClient) -> None:
    response = client.get("/v1/post", params={"url": "https://www.youtube.com/@mkbhd"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == UnsupportedUrlError.code


def test_history_is_empty_on_a_fresh_database(client: TestClient) -> None:
    response = client.get("/v1/history/youtube/mkbhd")
    assert response.status_code == 200
    body = response.json()
    assert body["platform"] == "youtube"
    assert body["handle"] == "mkbhd"
    assert body["points"] == []
