"""Instagram adapter.

Instagram is the most hostile of the four for anonymous HTML access. The adapter
therefore tries, in order:

1. the public web profile JSON endpoint (``i.instagram.com/api/v1``) with the
   web app id header - this is plain HTTP, not a browser;
2. the embedded ``xdt_api__v1__users__web_profile_info`` blob inside the page;
3. ``og:`` meta tags, which are the most stable surface but only expose the
   three headline counters (followers / following / posts).

Without cookies, expect occasional ``partial`` results and rate limiting.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from core.errors import BlockedError, NotFoundError, ScrapeError, UpstreamError
from models.schemas import (
    Confidence,
    Platform,
    PlatformCapabilities,
    Post,
    PostMetrics,
    Profile,
    ProfileMetrics,
    ResultStatus,
    ScrapeResult,
    Source,
)
from platforms.base import BaseAdapter, ParsedUrl
from services.human import clean_text, parse_count, parse_duration
from services.jsonblob import (
    dig,
    find_all_json_after,
    find_json_after,
    loads_or_none,
    og_tags,
)

_RESERVED_PATHS = {
    "p",
    "reel",
    "reels",
    "tv",
    "stories",
    "explore",
    "accounts",
    "direct",
    "about",
    "developer",
    "legal",
    "api",
    "web",
    "graphql",
    "challenge",
    "emails",
    "session",
}

# Instagram serves its internal endpoints under underscore-prefixed paths
# (``/__a/``, ``/_u/``). Usernames may legitimately start with an underscore,
# so only the known internal paths are rejected instead of every ``_`` prefix.
_INTERNAL_PATHS = {"_u", "_n", "_a", "_r"}
_INTERNAL_PREFIXES = ("__",)

_SHORTCODE_RE = re.compile(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_\-]{5,})")

_PROFILE_DESC_RE = re.compile(
    r"([\d.,]+\s*[KMB]?)\s*(?:Followers?|seguidores?)\s*,\s*"
    r"(?:Following|seguindo)?\s*([\d.,]+\s*[KMB]?)\s*(?:Following|seguindo)?\s*,\s*"
    r"([\d.,]+\s*[KMB]?)\s*(?:Posts?|publica[çc][õo]es|posts?)",
    re.I,
)

_POST_DESC_RE = re.compile(
    r"([\d.,]+\s*[KMB]?)\s*(?:Likes?|curtidas?)\s*,\s*"
    r"([\d.,]+\s*[KMB]?)\s*(?:Comments?|coment[áa]rios?)",
    re.I,
)

_TITLE_HANDLE_RE = re.compile(r"\(@([A-Za-z0-9._]{1,64})\)")


class InstagramAdapter(BaseAdapter):
    platform = Platform.instagram
    capabilities = PlatformCapabilities(
        platform=Platform.instagram,
        supports_profile=True,
        supports_posts=True,
        supports_single_post=True,
        html_reliability="medium",
        profile_fields=[
            "followers",
            "following",
            "posts",
            "display_name",
            "bio",
            "avatar_url",
            "verified",
            "external_id",
        ],
        post_fields=["likes", "comments", "views", "published_at", "caption"],
        requires_cookies_for=["posts list", "engagement metrics per post", "story highlights"],
        notes=(
            "Anonymous access is rate limited aggressively. Configure "
            "COOKIES_INSTAGRAM_FILE and a residential proxy for reliable results."
        ),
    )

    # ------------------------------------------------------------------ detection
    def detect(self, url: str) -> ParsedUrl | None:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        host = (parsed.netloc or "").lower().removeprefix("www.").removeprefix("m.")
        if host not in {"instagram.com", "instagr.am"}:
            return None

        path = parsed.path.strip("/")
        if not path:
            return None

        match = _SHORTCODE_RE.search(f"/{path}")
        if match:
            return ParsedUrl(
                platform=self.platform,
                kind="post",
                original=url,
                canonical_url=f"https://www.instagram.com/p/{match.group(1)}/",
                resource_id=match.group(1),
            )

        segments = [segment for segment in path.split("/") if segment]
        if not segments:
            return None

        raw_handle = segments[0]
        lowered = raw_handle.lower()
        if (
            lowered in _RESERVED_PATHS
            or lowered in _INTERNAL_PATHS
            or raw_handle.startswith(_INTERNAL_PREFIXES)
        ):
            return None
        handle = self.clean_handle(raw_handle)
        if not handle:
            return None

        return ParsedUrl(
            platform=self.platform,
            kind="profile",
            original=url,
            canonical_url=f"https://www.instagram.com/{handle}/",
            handle=handle,
        )

    # ------------------------------------------------------------------ profile
    async def fetch_profile(
        self,
        parsed: ParsedUrl,
        *,
        include_posts: bool = True,
        posts_limit: int = 12,
        refresh: bool = False,
    ) -> ScrapeResult:
        suffix = f"profile:{int(include_posts)}:{posts_limit}"
        key = self.cache_key(parsed, suffix)
        return await self.cached(
            key,
            self.settings.cache_ttl_profile_seconds,
            lambda: self._fetch_profile_uncached(
                parsed, include_posts=include_posts, posts_limit=posts_limit
            ),
            refresh=refresh,
        )

    async def _fetch_profile_uncached(
        self, parsed: ParsedUrl, *, include_posts: bool, posts_limit: int
    ) -> ScrapeResult:
        assert parsed.handle is not None
        handle = parsed.handle

        result = ScrapeResult(
            platform=self.platform,
            kind="profile",
            url=parsed.original,
            status=ResultStatus.ok,
            source=Source.official_api,
            confidence=Confidence.high,
        )

        api_user = await self._fetch_web_profile(handle)
        if api_user is not None:
            profile, posts = _profile_from_api_user(api_user)
            if profile is not None:
                result.profile = profile
                result.posts = posts[:posts_limit] if include_posts else []
                self._finalize(result)
                return result
            result.add_warning("web profile endpoint returned no user payload")

        # Fallback: HTML + og-tags + embedded JSON
        try:
            response = await self.http.get(
                f"https://www.instagram.com/{handle}/",
                platform=self.platform.value,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Referer": "https://www.instagram.com/",
                },
            )
        except (BlockedError, NotFoundError, UpstreamError) as exc:
            result.status = ResultStatus.failed
            result.confidence = Confidence.low
            result.error = exc.to_dict() if hasattr(exc, "to_dict") else {"code": "upstream_error", "message": str(exc)}
            return result

        html = response.text
        login_wall = _looks_like_login_wall(html)
        if login_wall:
            result.add_warning("instagram served a login wall")

        profile = _profile_from_html(html, handle)
        if profile is None:
            if login_wall:
                # Anonymous HTML carries no profile data at all: report the
                # handle we were given plus an actionable auth error instead of
                # pretending the scrape failed outright.
                result.status = ResultStatus.partial
                result.source = Source.html
                result.confidence = Confidence.low
                result.profile = Profile(
                    platform=self.platform,
                    handle=handle,
                    url=f"https://www.instagram.com/{handle}/",
                    extra={"via": "login_wall"},
                )
                result.error = {
                    "code": "auth_required",
                    "message": (
                        "Instagram no longer exposes profile counters to anonymous "
                        "clients; provide session cookies via COOKIES_INSTAGRAM_FILE"
                    ),
                }
                result.add_warning(
                    "profile counters unavailable without an authenticated session"
                )
                self._finalize(result)
                return result

            result.status = ResultStatus.failed
            result.confidence = Confidence.low
            result.error = {
                "code": "parse_error",
                "message": "could not extract profile from HTML or web profile endpoint",
            }
            return result

        result.profile = profile
        result.source = Source.html
        result.confidence = Confidence.medium

        if include_posts and posts_limit > 0:
            result.posts = _posts_from_html(html, limit=posts_limit)
            if not result.posts:
                result.add_warning("post list requires cookies; only profile counters available")

        self._finalize(result)
        return result

    async def _fetch_web_profile(self, handle: str) -> dict[str, Any] | None:
        headers = {
            "x-ig-app-id": self.settings.instagram_app_id,
            "Accept": "*/*",
            "Referer": f"https://www.instagram.com/{handle}/",
            "X-Requested-With": "XMLHttpRequest",
            "X-ASBD-ID": "129477",
            "X-IG-WWW-Claim": "0",
        }
        try:
            response = await self.http.get(
                "https://i.instagram.com/api/v1/users/web_profile_info/",
                platform=self.platform.value,
                params={"username": handle},
                headers=headers,
                retries=2,
            )
        except ScrapeError as exc:
            self.log.info("instagram_web_profile_failed", handle=handle, error=str(exc))
            return None

        payload = loads_or_none(response.text)
        if not isinstance(payload, dict):
            return None
        user = dig(payload, "data", "user")
        return user if isinstance(user, dict) else None

    # ------------------------------------------------------------------ post
    async def fetch_post(self, parsed: ParsedUrl, *, refresh: bool = False) -> ScrapeResult:
        key = self.cache_key(parsed)
        return await self.cached(
            key,
            self.settings.cache_ttl_post_seconds,
            lambda: self._fetch_post_uncached(parsed),
            refresh=refresh,
        )

    async def _fetch_post_uncached(self, parsed: ParsedUrl) -> ScrapeResult:
        shortcode = parsed.resource_id
        assert shortcode is not None
        url = f"https://www.instagram.com/p/{shortcode}/"

        result = ScrapeResult(
            platform=self.platform,
            kind="post",
            url=parsed.original,
            status=ResultStatus.ok,
            source=Source.html,
            confidence=Confidence.medium,
        )

        try:
            response = await self.http.get(
                url,
                platform=self.platform.value,
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
        except (BlockedError, NotFoundError, UpstreamError) as exc:
            result.status = ResultStatus.failed
            result.confidence = Confidence.low
            result.error = exc.to_dict() if hasattr(exc, "to_dict") else {"code": "upstream_error", "message": str(exc)}
            return result

        html = response.text
        post = _post_from_embedded_json(html, shortcode) or _post_from_og(html, shortcode)

        if post is None:
            result.status = ResultStatus.failed
            result.confidence = Confidence.low
            result.error = {"code": "parse_error", "message": "post payload not found"}
            return result

        result.post = post
        if post.metrics.likes is None and post.metrics.comments is None:
            result.add_warning("engagement metrics hidden by Instagram for this post")
            result.confidence = Confidence.low
        return result

    # ------------------------------------------------------------------ finalize
    def _finalize(self, result: ScrapeResult) -> None:
        from services.normalize import finalize

        confidence = (
            Confidence.high
            if result.source == Source.official_api
            else Confidence.medium
            if result.profile and result.profile.metrics.followers is not None
            else Confidence.low
        )
        finalize(result, required=["followers"], confidence=confidence)


# ---------------------------------------------------------------------- helpers


def _looks_like_login_wall(html: str) -> bool:
    """Instagram's anonymous shell carries no ``og:description``.

    Profile counters only ever appear in that meta tag, so its absence on an
    Instagram document means the data is gated behind a session.
    """
    if "og:description" in html.lower():
        return False
    lowered = html[:4000].lower()
    return "instagram" in lowered


_PLACEHOLDER_TITLES = {"instagram", "instagram photos and videos", "login • instagram"}


def _profile_from_api_user(user: dict[str, Any]) -> tuple[Profile | None, list[Post]]:
    username = user.get("username")
    if not username:
        return None, []

    profile = Profile(
        platform=Platform.instagram,
        handle=username,
        url=f"https://www.instagram.com/{username}/",
        display_name=clean_text(user.get("full_name")),
        bio=clean_text(user.get("biography")),
        avatar_url=user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
        verified=user.get("is_verified"),
        external_id=user.get("id") or user.get("pk"),
        metrics=ProfileMetrics(
            followers=dig(user, "edge_followed_by", "count"),
            following=dig(user, "edge_follow", "count"),
            posts=dig(user, "edge_owner_to_timeline_media", "count"),
        ),
        extra={
            "is_private": user.get("is_private"),
            "is_business": user.get("is_business_account"),
            "category": user.get("category_name"),
            "via": "web_profile_info",
        },
    )

    posts: list[Post] = []
    edges = dig(user, "edge_owner_to_timeline_media", "edges") or []
    for edge in edges:
        node = edge.get("node") if isinstance(edge, dict) else None
        post = _post_from_media_node(node)
        if post is not None:
            posts.append(post)
    return profile, posts


def _profile_from_html(html: str, handle: str) -> Profile | None:
    tags = og_tags(html)
    description = tags.get("description") or ""
    title = tags.get("title") or ""
    if title.strip().lower() in _PLACEHOLDER_TITLES:
        # The logged-out shell only carries the generic "Instagram" title.
        title = ""

    followers = following = posts_count = None
    match = _PROFILE_DESC_RE.search(description)
    if match:
        followers = parse_count(match.group(1))
        following = parse_count(match.group(2))
        posts_count = parse_count(match.group(3))

    embedded = _embedded_profile(html)
    if embedded is not None:
        profile, _ = _profile_from_api_user(embedded)
        if profile is not None:
            if profile.metrics.followers is None:
                profile.metrics.followers = followers
            profile.metrics.following = profile.metrics.following or following
            profile.metrics.posts = profile.metrics.posts or posts_count
            return profile

    if followers is None and posts_count is None and not title:
        return None

    handle_match = _TITLE_HANDLE_RE.search(title)
    resolved_handle = handle_match.group(1) if handle_match else handle

    display_name = re.sub(r"\s*\(@[^)]+\).*$", "", title).strip() or None
    display_name = clean_text(display_name)

    bio = None
    bio_match = re.search(r"-\s*See Instagram photos and videos.*$", description, re.I)
    if bio_match:
        bio = None

    return Profile(
        platform=Platform.instagram,
        handle=resolved_handle,
        url=f"https://www.instagram.com/{resolved_handle}/",
        display_name=display_name,
        bio=bio,
        avatar_url=tags.get("image"),
        verified=None,
        metrics=ProfileMetrics(
            followers=followers,
            following=following,
            posts=posts_count,
        ),
        extra={"via": "og_tags"},
    )


def _embedded_profile(html: str) -> dict[str, Any] | None:
    """Extract ``xdt_api__v1__users__web_profile_info`` from the page JSON."""
    payload = find_json_after(html, "xdt_api__v1__users__web_profile_info")
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("user"), dict):
            return data["user"]
        if "username" in payload:
            return payload

    for candidate in find_all_json_after(html, '"full_name"', limit=10):
        if isinstance(candidate, dict) and "username" in candidate:
            return candidate
    return None


def _posts_from_html(html: str, *, limit: int) -> list[Post]:
    embedded = _embedded_profile(html)
    if embedded is None:
        return []
    _, posts = _profile_from_api_user(embedded)
    return posts[:limit]


def _post_from_media_node(node: Any) -> Post | None:
    if not isinstance(node, dict):
        return None
    shortcode = node.get("shortcode")
    if not shortcode:
        return None
    taken_at = node.get("taken_at_timestamp") or node.get("taken_at")
    published_at = None
    if isinstance(taken_at, (int, float)) and taken_at > 0:
        published_at = datetime.fromtimestamp(int(taken_at), tz=UTC)

    caption = clean_text(dig(node, "edge_media_to_caption", "edges", 0, "node", "text"))
    is_video = bool(node.get("is_video"))

    return Post(
        platform=Platform.instagram,
        id=str(node.get("id") or shortcode),
        url=f"https://www.instagram.com/p/{shortcode}/",
        caption=caption,
        thumbnail_url=node.get("thumbnail_src") or node.get("display_url"),
        published_at=published_at,
        duration_seconds=parse_duration(node.get("video_duration")),
        media_type="reel" if node.get("product_type") == "clips" else ("video" if is_video else "image"),
        metrics=PostMetrics(
            views=node.get("video_view_count") or node.get("video_play_count"),
            likes=dig(node, "edge_liked_by", "count") or node.get("like_count"),
            comments=dig(node, "edge_media_to_comment", "count") or node.get("comment_count"),
        ),
        extra={"via": "web_profile_info"},
    )


def _post_from_embedded_json(html: str, shortcode: str) -> Post | None:
    payload = find_json_after(html, "xdt_api__v1__media__shortcode__web_info")
    items = None
    if isinstance(payload, dict):
        items = payload.get("items")
    if not isinstance(items, list) or not items:
        return None
    item = items[0]
    if not isinstance(item, dict):
        return None

    raw_user = item.get("user")
    user: dict[str, Any] = raw_user if isinstance(raw_user, dict) else {}
    taken_at = item.get("taken_at")
    published_at = None
    if isinstance(taken_at, (int, float)) and taken_at > 0:
        published_at = datetime.fromtimestamp(int(taken_at), tz=UTC)

    code = item.get("code") or shortcode
    return Post(
        platform=Platform.instagram,
        id=str(item.get("pk") or item.get("id") or code),
        url=f"https://www.instagram.com/p/{code}/",
        caption=clean_text(dig(item, "caption", "text")),
        thumbnail_url=dig(item, "image_versions2", "candidates", 0, "url"),
        published_at=published_at,
        duration_seconds=parse_duration(item.get("video_duration")),
        media_type="reel" if item.get("product_type") == "clips" else (
            "video" if item.get("media_type") == 2 else "image"
        ),
        metrics=PostMetrics(
            views=item.get("play_count") or item.get("view_count"),
            likes=item.get("like_count"),
            comments=item.get("comment_count"),
        ),
        extra={"author": user.get("username"), "via": "embedded_json"},
    )


def _post_from_og(html: str, shortcode: str) -> Post | None:
    tags = og_tags(html)
    description = tags.get("description") or ""
    if not description:
        return None

    likes = comments = None
    match = _POST_DESC_RE.search(description)
    if match:
        likes = parse_count(match.group(1))
        comments = parse_count(match.group(2))

    caption = None
    caption_match = re.search(r'on Instagram:\s*[“"](.+?)[”"]\s*\.?\s*$', description, re.S)
    if caption_match:
        caption = clean_text(caption_match.group(1))

    author = None
    author_match = re.search(r"-\s*([A-Za-z0-9._]+)\s+on\s+\w+\s+\d", description)
    if author_match:
        author = author_match.group(1)

    if likes is None and comments is None and not caption:
        return None

    return Post(
        platform=Platform.instagram,
        id=shortcode,
        url=f"https://www.instagram.com/p/{shortcode}/",
        caption=caption,
        thumbnail_url=tags.get("image"),
        media_type="unknown",
        metrics=PostMetrics(likes=likes, comments=comments),
        extra={"author": author, "via": "og_tags"},
    )
