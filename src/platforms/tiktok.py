"""TikTok adapter.

TikTok ships its whole page state in
``<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">``, so no browser is needed.
The official Display API is not usable for arbitrary third-party accounts, and
the private ``/api/`` endpoints require request signing, so HTML is the path.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from core.errors import BlockedError, NotFoundError, UpstreamError
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
from services.human import clean_text, parse_count
from services.jsonblob import (
    dig,
    find_key,
    loads_or_none,
    script_json,
    walk,
)

_VIDEO_ID_RE = re.compile(r"/video/(\d{6,})")
_REHYDRATION_ID = "__UNIVERSAL_DATA_FOR_REHYDRATION__"

_NOT_FOUND_CODES = {10204, 10216, 10222}


class TikTokAdapter(BaseAdapter):
    platform = Platform.tiktok
    capabilities = PlatformCapabilities(
        platform=Platform.tiktok,
        supports_profile=True,
        supports_posts=True,
        supports_single_post=True,
        html_reliability="medium",
        profile_fields=[
            "followers",
            "following",
            "posts",
            "likes_total",
            "display_name",
            "bio",
            "avatar_url",
            "verified",
            "external_id",
        ],
        post_fields=["views", "likes", "comments", "shares", "duration_seconds", "published_at"],
        requires_cookies_for=["region-restricted content", "private accounts"],
        notes=(
            "Parsed from __UNIVERSAL_DATA_FOR_REHYDRATION__. Video lists on the "
            "profile page are usually capped to the first page of results."
        ),
    )

    # ------------------------------------------------------------------ detection
    def detect(self, url: str) -> ParsedUrl | None:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        host = (parsed.netloc or "").lower().removeprefix("www.").removeprefix("m.")
        if host not in {"tiktok.com", "vm.tiktok.com", "vt.tiktok.com"}:
            return None

        path = parsed.path.strip("/")

        if host in {"vm.tiktok.com", "vt.tiktok.com"} or path.startswith("t/"):
            code = path.split("/")[-1]
            if not code:
                return None
            return ParsedUrl(
                platform=self.platform,
                kind="profile",
                original=url,
                canonical_url=url,
                handle=None,
                extra={"short_code": code},
            )

        match = _VIDEO_ID_RE.search(path)
        if match:
            handle = _handle_from_path(path)
            return ParsedUrl(
                platform=self.platform,
                kind="post",
                original=url,
                canonical_url=f"https://www.tiktok.com/@{handle or 'i'}/video/{match.group(1)}",
                handle=handle,
                resource_id=match.group(1),
            )

        handle = _handle_from_path(path)
        if handle:
            return ParsedUrl(
                platform=self.platform,
                kind="profile",
                original=url,
                canonical_url=f"https://www.tiktok.com/@{handle}",
                handle=handle,
            )
        return None

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
        result = ScrapeResult(
            platform=self.platform,
            kind="profile",
            url=parsed.original,
            status=ResultStatus.ok,
            source=Source.embedded_json,
            confidence=Confidence.medium,
        )

        target = parsed
        if parsed.extra.get("short_code"):
            resolved = await self._resolve_short_link(parsed.original)
            if resolved is None:
                result.status = ResultStatus.failed
                result.error = {"code": "not_found", "message": "could not resolve short link"}
                return result
            target = resolved

        if target.handle is None:
            result.status = ResultStatus.failed
            result.error = {"code": "unsupported_url", "message": "missing TikTok handle"}
            return result

        url = f"https://www.tiktok.com/@{target.handle}"
        try:
            response = await self.http.get(
                url,
                platform=self.platform.value,
                headers={"Accept": "text/html"},
            )
        except (BlockedError, NotFoundError, UpstreamError) as exc:
            return await self._failover(result, target, exc)

        html = response.text
        payload = self._rehydration_payload(html)

        user_detail = find_key(payload, "webapp.user-detail") if payload else None
        if isinstance(user_detail, dict):
            status_code = user_detail.get("statusCode")
            if status_code in _NOT_FOUND_CODES:
                result.status = ResultStatus.failed
                result.confidence = Confidence.low
                result.error = {"code": "not_found", "message": "profile unavailable"}
                return result

        user = _find_user(user_detail) if user_detail else None
        stats = _find_stats(user_detail) if user_detail else None

        if user is None:
            user = _find_user(payload) if payload else None
        if stats is None:
            stats = _find_stats(payload) if payload else None

        if user is None:
            oembed = await self._oembed(url)
            if oembed is not None:
                result.profile = oembed
                result.source = Source.oembed
                result.confidence = Confidence.low
                result.add_warning("only oEmbed metadata available (no follower counts)")
                if include_posts and posts_limit > 0:
                    result.posts = await self._posts_from_payload(payload, posts_limit)
                return result

            result.status = ResultStatus.failed
            result.confidence = Confidence.low
            result.error = {
                "code": "parse_error",
                "message": "user payload not found; TikTok may be serving a login wall",
            }
            return result

        handle = user.get("uniqueId") or target.handle
        result.profile = Profile(
            platform=self.platform,
            handle=handle,
            url=f"https://www.tiktok.com/@{handle}",
            display_name=clean_text(user.get("nickname")),
            bio=clean_text(user.get("signature")),
            avatar_url=user.get("avatarLarger") or user.get("avatarMedium") or user.get("avatarThumb"),
            verified=user.get("verified"),
            external_id=user.get("id") or user.get("secUid"),
            metrics=ProfileMetrics(
                followers=_int(stats, "followerCount"),
                following=_int(stats, "followingCount"),
                posts=_int(stats, "videoCount"),
                likes_total=_int(stats, "heartCount") or _int(stats, "heart"),
            ),
            extra={
                "region": user.get("region"),
                "private_account": user.get("privateAccount"),
                "sec_uid": user.get("secUid"),
            },
        )

        if include_posts and posts_limit > 0:
            result.posts = await self._posts_from_payload(payload, posts_limit)
            if not result.posts:
                result.add_warning(
                    "TikTok only embeds the post list for signed-in sessions; "
                    "profile counters are available but posts are not "
                    "(set COOKIES_TIKTOK_FILE to enable)"
                )

        self._finalize(result)
        return result

    def _rehydration_payload(self, html: str) -> Any:
        payload = script_json(html, element_id=_REHYDRATION_ID)
        if payload is not None:
            return payload
        from services.jsonblob import find_json_after

        return find_json_after(html, "__UNIVERSAL_DATA_FOR_REHYDRATION__")

    async def _posts_from_payload(self, payload: Any, limit: int) -> list[Post]:
        if payload is None:
            return []
        posts: list[Post] = []
        seen: set[str] = set()

        for node in walk(payload):
            if not isinstance(node, dict) or "itemStruct" not in node:
                continue
            post = _post_from_item(node["itemStruct"])
            if post is None or post.url in seen:
                continue
            seen.add(post.url)
            posts.append(post)
            if len(posts) >= limit:
                break
        return posts

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
        video_id = parsed.resource_id
        assert video_id is not None

        result = ScrapeResult(
            platform=self.platform,
            kind="post",
            url=parsed.original,
            status=ResultStatus.ok,
            source=Source.embedded_json,
            confidence=Confidence.medium,
        )

        handle = parsed.handle or "i"
        url = f"https://www.tiktok.com/@{handle}/video/{video_id}"

        try:
            response = await self.http.get(
                url,
                platform=self.platform.value,
                headers={"Accept": "text/html"},
            )
        except (BlockedError, NotFoundError, UpstreamError) as exc:
            return await self._failover_post(result, parsed, exc)

        payload = self._rehydration_payload(response.text)
        video_detail = find_key(payload, "webapp.video-detail") if payload else None

        if isinstance(video_detail, dict) and video_detail.get("statusCode") in _NOT_FOUND_CODES:
            result.status = ResultStatus.failed
            result.error = {"code": "not_found", "message": "video unavailable"}
            return result

        item = _find_item_struct(video_detail) if video_detail else None
        if item is None and payload is not None:
            item = _find_item_struct(payload)

        post = _post_from_item(item) if item else None
        if post is None:
            oembed = await self._oembed(url)
            if oembed is not None:
                post = Post(
                    platform=self.platform,
                    id=video_id,
                    url=url,
                    caption=oembed.display_name,
                    thumbnail_url=oembed.avatar_url,
                    media_type="video",
                    extra={"via": "oembed"},
                )
                result.source = Source.oembed
                result.add_warning("only oEmbed metadata available (no engagement metrics)")

        if post is None:
            result.status = ResultStatus.failed
            result.confidence = Confidence.low
            result.error = {"code": "parse_error", "message": "video payload not found"}
            return result

        result.post = post
        result.confidence = (
            Confidence.high if result.source == Source.embedded_json else Confidence.low
        )
        return result

    # ------------------------------------------------------------------ fallbacks
    async def _resolve_short_link(self, url: str) -> ParsedUrl | None:
        try:
            response = await self.http.get(url, platform=self.platform.value)
        except (BlockedError, NotFoundError, UpstreamError):
            return None
        return self.detect(response.final_url or url)

    async def _oembed(self, url: str) -> Profile | None:
        try:
            response = await self.http.get(
                "https://www.tiktok.com/oembed",
                platform=self.platform.value,
                params={"url": url},
                headers={"Accept": "application/json"},
            )
        except (BlockedError, NotFoundError, UpstreamError):
            return None
        payload = loads_or_none(response.text)
        if not isinstance(payload, dict):
            return None
        handle = _handle_from_text(payload.get("author_url")) or payload.get("author_name")
        return Profile(
            platform=self.platform,
            handle=handle or "unknown",
            url=payload.get("author_url") or url,
            display_name=clean_text(payload.get("author_name")),
            avatar_url=payload.get("thumbnail_url"),
            metrics=ProfileMetrics(),
            extra={"via": "oembed"},
        )

    async def _failover(
        self, result: ScrapeResult, parsed: ParsedUrl, exc: Exception
    ) -> ScrapeResult:
        self.log.warning("tiktok_profile_failed", url=parsed.original, error=str(exc))
        oembed = await self._oembed(parsed.canonical_url)
        if oembed is not None:
            result.profile = oembed
            result.source = Source.oembed
            result.confidence = Confidence.low
            result.add_warning("upstream blocked; only oEmbed metadata available")
            return result
        result.status = ResultStatus.failed
        result.confidence = Confidence.low
        result.error = exc.to_dict() if hasattr(exc, "to_dict") else {"code": "upstream_error", "message": str(exc)}
        return result

    async def _failover_post(
        self, result: ScrapeResult, parsed: ParsedUrl, exc: Exception
    ) -> ScrapeResult:
        self.log.warning("tiktok_post_failed", url=parsed.original, error=str(exc))
        result.status = ResultStatus.failed
        result.confidence = Confidence.low
        result.error = exc.to_dict() if hasattr(exc, "to_dict") else {"code": "upstream_error", "message": str(exc)}
        return result

    # ------------------------------------------------------------------ finalize
    def _finalize(self, result: ScrapeResult) -> None:
        from services.normalize import finalize

        finalize(
            result,
            required=["followers", "posts"],
            confidence=Confidence.high
            if result.posts and result.profile and result.profile.metrics.followers is not None
            else Confidence.medium,
        )


# ---------------------------------------------------------------------- helpers


def _handle_from_path(path: str) -> str | None:
    for segment in path.split("/"):
        if segment.startswith("@"):
            return segment[1:].split("?")[0]
    return None


def _handle_from_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.search(r"@([A-Za-z0-9._]{2,64})", value)
    return match.group(1) if match else None


def _int(stats: Any, key: str) -> int | None:
    if not isinstance(stats, dict):
        return None
    return _coerce_count(stats.get(key))


def _coerce_count(value: Any) -> int | None:
    """Coerce a TikTok counter to a sane non-negative int.

    TikTok ships the same counter twice: a display block with rounded ints and a
    ``userInfo.stats`` block with exact values serialized as strings. The display
    block also stores big counters (e.g. ``heartCount`` above 2^31) as 32-bit
    signed ints, which wrap into negative numbers. Prefer string values and
    repair wrapped negatives.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
        return parse_count(text) if text else None
    if isinstance(value, int):
        if value < 0:
            # 32-bit signed wraparound from the display stats block.
            return value + 2**32
        return value
    return None


def _find_stats(root: Any) -> dict[str, Any] | None:
    """Merge every stats block, preferring exact string values over display ints."""
    merged: dict[str, Any] = {}
    found = False
    for node in walk(root):
        if not isinstance(node, dict):
            continue
        candidates: list[Any] = []
        if isinstance(node.get("stats"), dict):
            candidates.append(node["stats"])
        user_info = node.get("userInfo")
        if isinstance(user_info, dict) and isinstance(user_info.get("stats"), dict):
            candidates.append(user_info["stats"])
        for stats in candidates:
            if "followerCount" not in stats:
                continue
            found = True
            for key, value in stats.items():
                current = merged.get(key)
                wrapped = isinstance(current, int) and current < 0 and _coerce_count(value)
                exact_string = (
                    not isinstance(current, str) and isinstance(value, str) and value.isdigit()
                )
                if current is None or wrapped or exact_string:
                    merged[key] = value
    return merged if found else None


def _find_user(root: Any) -> dict[str, Any] | None:
    """Locate the ``user`` object (distinguishing it from ``itemStruct.author``)."""
    for node in walk(root):
        if not isinstance(node, dict):
            continue
        candidate = node.get("userInfo")
        if isinstance(candidate, dict) and isinstance(candidate.get("user"), dict):
            return candidate["user"]
        if {"uniqueId", "nickname"} <= node.keys():
            return node
    return None


def _find_item_struct(root: Any) -> dict[str, Any] | None:
    for node in walk(root):
        if isinstance(node, dict) and isinstance(node.get("itemStruct"), dict):
            return node["itemStruct"]
        if isinstance(node, dict) and "desc" in node and "video" in node and "stats" in node:
            return node
    return None


def _post_from_item(item: Any) -> Post | None:
    if not isinstance(item, dict):
        return None
    video_id = item.get("id") or item.get("aweme_id")
    if not video_id:
        return None
    raw_author = item.get("author")
    author: dict[str, Any] = raw_author if isinstance(raw_author, dict) else {}
    handle = author.get("uniqueId") or "i"
    raw_stats = item.get("stats")
    stats: dict[str, Any] = raw_stats if isinstance(raw_stats, dict) else {}
    raw_video = item.get("video")
    video: dict[str, Any] = raw_video if isinstance(raw_video, dict) else {}

    create_time = item.get("createTime")
    published_at = None
    if isinstance(create_time, (int, float)) and create_time > 0:
        published_at = datetime.fromtimestamp(int(create_time), tz=UTC)

    duration = video.get("duration")
    return Post(
        platform=Platform.tiktok,
        id=str(video_id),
        url=f"https://www.tiktok.com/@{handle}/video/{video_id}",
        caption=clean_text(item.get("desc")),
        thumbnail_url=video.get("cover") or video.get("originCover") or video.get("dynamicCover"),
        published_at=published_at,
        duration_seconds=float(duration) if isinstance(duration, (int, float)) else None,
        media_type="video",
        metrics=PostMetrics(
            views=_int(stats, "playCount"),
            likes=_int(stats, "diggCount"),
            comments=_int(stats, "commentCount"),
            shares=_int(stats, "shareCount"),
            saves=_int(stats, "collectCount"),
            plays=_int(stats, "playCount"),
        ),
        extra={
            "author": handle,
            "author_id": author.get("id"),
            "music": clean_text(dig(item, "music", "title")),
            "via": "embedded_json",
        },
    )
