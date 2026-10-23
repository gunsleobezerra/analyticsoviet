"""Model Context Protocol (MCP) server for AnalyticsSoviet.

Exposes social media scraping, profile intelligence, and metrics history
as MCP tools and resources for AI assistants (Claude, Cursor, Antigravity, etc.).
"""

from __future__ import annotations

import asyncio
import json

from mcp.server.mcpserver import MCPServer

from api.deps import build_state
from api.routes.scrape import _dispatch_url
from core.cache import create_cache
from core.config import get_settings
from core.http import HttpClient
from db.session import Database
from db.snapshots import fetch_history
from models.schemas import Platform, ScrapeRequest

# Initialize MCPServer
mcp = MCPServer(
    "AnalyticsSoviet",
    instructions=(
        "AnalyticsSoviet MCP Server provides real-time social media scraping "
        "and metrics analysis for YouTube, TikTok, Instagram, and Facebook without official API keys."
    ),
)


_STATE = None


async def _get_app_state():
    global _STATE
    if _STATE is not None:
        return _STATE
    settings = get_settings()
    http = HttpClient(settings)
    await http.start()
    cache = create_cache(settings)
    db = Database(settings)
    await db.start()
    _STATE = build_state(settings, http, cache, db)
    return _STATE


@mcp.tool(
    name="scrape_url",
    description=(
        "Scrape a social media profile or post URL from Instagram, YouTube, Facebook, or TikTok. "
        "Extracts followers, subscribers, post counts, captions, engagement metrics, and recent posts."
    ),
)
async def scrape_url(
    url: str,
    include_posts: bool = True,
    posts_limit: int = 6,
    refresh: bool = False,
) -> str:
    """Scrape a single profile or post URL and return structured metrics."""
    state = await _get_app_state()
    req = ScrapeRequest(
        urls=[url],
        include_posts=include_posts,
        posts_limit=posts_limit,
        refresh=refresh,
    )
    result = await _dispatch_url(state, url, req)
    return result.model_dump_json(indent=2)


@mcp.tool(
    name="scrape_batch",
    description="Scrape multiple social media URLs concurrently across supported platforms.",
)
async def scrape_batch(
    urls: list[str],
    include_posts: bool = True,
    posts_limit: int = 3,
    refresh: bool = False,
) -> str:
    """Scrape a list of URLs in parallel."""
    state = await _get_app_state()
    req = ScrapeRequest(
        urls=urls,
        include_posts=include_posts,
        posts_limit=posts_limit,
        refresh=refresh,
    )
    results = await asyncio.gather(
        *(_dispatch_url(state, url, req) for url in urls)
    )
    data = [r.model_dump() for r in results]
    return json.dumps(data, indent=2, default=str)


@mcp.tool(
    name="get_profile_history",
    description="Retrieve recorded metric snapshots and follower/post growth delta for a profile.",
)
async def profile_history(platform: str, handle: str) -> str:
    """Fetch tracked follower/growth history for a profile."""
    state = await _get_app_state()
    plat = Platform(platform.lower())
    response = await fetch_history(state.db, plat, handle)
    return response.model_dump_json(indent=2)


@mcp.tool(
    name="list_supported_platforms",
    description="List all enabled platforms and their scraping capabilities (profiles, posts, single posts).",
)
async def list_platforms() -> str:
    """List platforms and capabilities matrix."""
    state = await _get_app_state()
    platforms = [
        adapter.capabilities.model_dump()
        for adapter in state.registry.adapters.values()
    ]
    return json.dumps(platforms, indent=2)


def main():
    """Run the MCP server via standard stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
