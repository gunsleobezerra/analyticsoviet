"""Diagnose why Instagram returns empty profiles.

Checks, in order:
1. Is a cookie jar resolved for instagram and does it carry ``sessionid``?
2. Does the web_profile_info endpoint answer with a user payload?
3. Does the HTML profile page still expose ``og:description``?

Run inside the container:

    docker exec analyticsoviet-api-1 python scripts/diag_instagram.py [handle]
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from core.config import get_settings
from services.jsonblob import loads_or_none


async def main(handle: str) -> None:
    settings = get_settings()
    path = settings.cookies_file_for("instagram")
    print(f"cookie file: {path}")

    jar: dict[str, str] = {}
    if path is not None:
        for line in path.read_text().splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                jar[parts[5]] = parts[6]
    print(f"cookie names: {sorted(jar)}")
    print(f"has sessionid: {'sessionid' in jar}")

    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    async with httpx.AsyncClient(cookies=jar, follow_redirects=True, timeout=20) as client:
        print("\n--- web_profile_info (with cookies) ---")
        r = await client.get(
            "https://i.instagram.com/api/v1/users/web_profile_info/",
            params={"username": handle},
            headers={
                "x-ig-app-id": settings.instagram_app_id,
                "User-Agent": ua,
                "Accept": "*/*",
                "Referer": f"https://www.instagram.com/{handle}/",
                "X-Requested-With": "XMLHttpRequest",
                "X-ASBD-ID": "129477",
                "X-IG-WWW-Claim": "0",
            },
        )
        print(f"status: {r.status_code} len: {len(r.text)}")
        payload = loads_or_none(r.text)
        user = payload.get("data", {}).get("user") if isinstance(payload, dict) else None
        if isinstance(user, dict):
            print(f"user: {user.get('username')}")
            print(f"  followers: {user.get('edge_followed_by', {}).get('count')}")
            print(f"  following: {user.get('edge_follow', {}).get('count')}")
            print(f"  posts: {user.get('edge_owner_to_timeline_media', {}).get('count')}")
        else:
            print(f"no user payload; body head: {r.text[:300]!r}")

        print("\n--- profile HTML (with cookies) ---")
        h = await client.get(
            f"https://www.instagram.com/{handle}/",
            headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml"},
        )
        print(f"status: {h.status_code} len: {len(h.text)}")
        print(f"has og:description: {'og:description' in h.text}")
        print(f"has login form: {'loginForm' in h.text or '/accounts/login' in h.text}")
        for marker in ("xdt_api__v1__users__web_profile_info", "edge_followed_by", "challenge"):
            print(f"  marker {marker!r}: {marker in h.text}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "orient.marxista"))
