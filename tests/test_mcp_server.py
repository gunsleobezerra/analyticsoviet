from __future__ import annotations

import pytest

from mcp_server import list_platforms, profile_history, scrape_url


@pytest.mark.asyncio
async def test_mcp_list_platforms() -> None:
    result = await list_platforms()
    assert "youtube" in result
    assert "instagram" in result
    assert "facebook" in result
    assert "tiktok" in result


@pytest.mark.asyncio
async def test_mcp_scrape_url_invalid() -> None:
    result = await scrape_url("https://google.com")
    assert "unsupported_url" in result or "failed" in result
