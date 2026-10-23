"""Live smoke test: hit the real platforms once and print the normalized result.

Usage::

    .venv/bin/python scripts/smoke_live.py [--platforms youtube,tiktok,instagram,facebook]

Requires network access. Not part of the normal test suite (see ``tests/live``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core.cache import create_cache  # noqa: E402
from core.config import Settings  # noqa: E402
from core.http import HttpClient  # noqa: E402
from platforms.base import AdapterContext  # noqa: E402
from platforms.registry import AdapterRegistry  # noqa: E402

DEFAULT_TARGETS: dict[str, str] = {
    "youtube": "https://www.youtube.com/@mkbhd",
    "tiktok": "https://www.tiktok.com/@khaby.lame",
    "instagram": "https://www.instagram.com/nasa/",
    "facebook": "https://www.facebook.com/nasa",
}


def _summarize(result: Any) -> dict[str, Any]:
    profile = result.profile
    if profile is None:
        return {
            "platform": str(result.platform),
            "status": str(result.status),
            "source": str(result.source),
            "confidence": str(result.confidence),
            "warnings": list(result.warnings),
            "error": result.error,
        }
    metrics = profile.metrics
    return {
        "platform": str(result.platform),
        "status": str(result.status),
        "source": str(result.source),
        "confidence": str(result.confidence),
        "handle": profile.handle,
        "display_name": profile.display_name,
        "followers": metrics.followers,
        "following": metrics.following,
        "posts_total": metrics.posts,
        "likes_total": metrics.likes_total,
        "posts_scraped": len(result.posts),
        "post_preview": [
            {
                "id": post.id,
                "views": post.metrics.views,
                "likes": post.metrics.likes,
                "duration": post.duration_seconds,
                "published": str(post.published_at) if post.published_at else None,
                "title": (post.caption or "")[:70],
            }
            for post in result.posts[:3]
        ],
        "warnings": list(result.warnings),
        "error": result.error,
    }


async def run(platforms: list[str]) -> int:
    settings = Settings()
    http = HttpClient(settings)
    await http.start()
    cache = create_cache(settings)
    registry = AdapterRegistry.default(AdapterContext(settings=settings, http=http, cache=cache))

    failures = 0
    try:
        for platform in platforms:
            url = DEFAULT_TARGETS[platform]
            parsed = registry.detect(url)
            if parsed is None:
                print(f"[{platform}] detect() returned None for {url}")
                failures += 1
                continue
            adapter = registry.get(parsed.platform)
            try:
                result = await adapter.fetch_profile(parsed, include_posts=True, posts_limit=3, refresh=True)
            except Exception as exc:
                print(f"[{platform}] raised {type(exc).__name__}: {exc}")
                failures += 1
                continue
            print(f"[{platform}]")
            print(json.dumps(_summarize(result), indent=2, ensure_ascii=False, default=str))
            if str(result.status) == "failed":
                failures += 1
    finally:
        await http.aclose()

    print(f"\n{len(platforms) - failures}/{len(platforms)} platforms returned non-failed results")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--platforms",
        default=",".join(DEFAULT_TARGETS),
        help="Comma separated subset of: " + ", ".join(DEFAULT_TARGETS),
    )
    args = parser.parse_args()
    platforms = [item.strip() for item in args.platforms.split(",") if item.strip()]
    unknown = [item for item in platforms if item not in DEFAULT_TARGETS]
    if unknown:
        parser.error(f"unknown platform(s): {', '.join(unknown)}")
    return asyncio.run(run(platforms))


if __name__ == "__main__":
    raise SystemExit(main())
