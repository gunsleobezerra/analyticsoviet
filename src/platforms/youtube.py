"""YouTube adapter.

Strategy, in order of preference:

1. ``ytInitialData`` / ``ytInitialPlayerResponse`` embedded JSON (no browser);
2. ``yt-dlp`` in dump mode (pure HTTP, still no browser);
3. YouTube Data API v3 when ``YOUTUBE_API_KEY`` is configured.

Channel and video pages both expose the full state server-side, which makes
YouTube by far the most reliable of the four platforms.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from core.errors import BlockedError, NotFoundError, ParseError, UpstreamError
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
from services.human import (
    clean_text,
    parse_count,
    parse_duration,
    parse_relative_date,
)
from services.jsonblob import (
    dig,
    find_all,
    find_json_after,
    find_key,
    og_tags,
    text_content,
)

_SUBSCRIBER_RE = re.compile(r"([\d.,]+\s*[KMB]?)\s*(?:subscribers?|inscritos?)", re.I)
_VIDEO_COUNT_RE = re.compile(r"([\d.,]+\s*[KMB]?)\s*(?:videos?|vídeos?)", re.I)
_VIEW_RE = re.compile(r"([\d.,]+\s*[KMB]?)\s*(?:views?|visualiza[çc][õo]es)", re.I)
_LIKES_RE = re.compile(r"([\d.,]+\s*[KMB]?)\s*(?:likes?|curtidas?)", re.I)
_HANDLE_RE = re.compile(r"@([A-Za-z0-9._\-]{3,64})")

_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_\-]{22}$")

# Relative dates as rendered by YouTube ("há 4 meses", "2 days ago", "1年前").
_REL_DATE_HINT_RE = re.compile(
    r"h[áa]\s+\d|ago|yesterday|today|ontem|hoje|前|週間|か月|年前",
    re.I,
)
# Duration badges on thumbnails: "17:42", "1:02:33".
_DURATION_RE = re.compile(r"^\d{1,2}(?::\d{2}){1,2}$")


class YouTubeAdapter(BaseAdapter):
    platform = Platform.youtube
    capabilities = PlatformCapabilities(
        platform=Platform.youtube,
        supports_profile=True,
        supports_posts=True,
        supports_single_post=True,
        html_reliability="high",
        profile_fields=[
            "followers",
            "posts",
            "display_name",
            "bio",
            "avatar_url",
            "external_id",
            "views_total",
        ],
        post_fields=[
            "views",
            "likes",
            "comments",
            "duration_seconds",
            "published_at",
            "caption",
            "thumbnail_url",
        ],
        requires_cookies_for=[],
        notes=(
            "Subscriber/video counts come from ytInitialData; per-video likes and "
            "comment counts from the watch page. yt-dlp is used as fallback."
        ),
    )

    # ------------------------------------------------------------------ detection
    def detect(self, url: str) -> ParsedUrl | None:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        host = (parsed.netloc or "").lower().removeprefix("www.").removeprefix("m.")
        if host not in {"youtube.com", "youtu.be", "music.youtube.com"}:
            return None

        path = parsed.path.strip("/")

        if host == "youtu.be":
            video_id: str | None = path.split("/")[0]
            if _valid_video_id(video_id):
                return self._video_parsed(video_id, url)
            return None

        if path in {"watch", "watch/"}:  # pragma: no cover - defensive
            video_id = self.query_param(url, "v")
            return self._video_parsed(video_id, url) if _valid_video_id(video_id) else None

        segments = [segment for segment in path.split("/") if segment]
        if not segments:
            return None

        first = segments[0]

        if first == "watch":
            video_id = self.query_param(url, "v")
            return self._video_parsed(video_id, url) if _valid_video_id(video_id) else None

        if first in {"shorts", "live", "embed", "v"} and len(segments) >= 2:
            video_id = segments[1]
            if _valid_video_id(video_id):
                return self._video_parsed(video_id, url)
            return None

        if first == "channel" and len(segments) >= 2:
            handle = segments[1]
            return ParsedUrl(
                platform=self.platform,
                kind="profile",
                original=url,
                canonical_url=f"https://www.youtube.com/channel/{handle}",
                handle=handle,
                extra={"channel_id": handle},
            )

        if first in {"c", "user"} and len(segments) >= 2:
            legacy_handle = self.clean_handle(segments[1])
            if not legacy_handle:
                return None
            return ParsedUrl(
                platform=self.platform,
                kind="profile",
                original=url,
                canonical_url=f"https://www.youtube.com/{first}/{legacy_handle}",
                handle=legacy_handle,
                extra={"legacy_path": first},
            )

        if first.startswith("@"):
            at_handle = self.clean_handle(first)
            if not at_handle:
                return None
            return ParsedUrl(
                platform=self.platform,
                kind="profile",
                original=url,
                canonical_url=f"https://www.youtube.com/@{at_handle}",
                handle=at_handle,
            )

        # bare handle in the path: youtube.com/handle
        bare_handle = self.clean_handle(first)
        if bare_handle and len(segments) == 1:
            return ParsedUrl(
                platform=self.platform,
                kind="profile",
                original=url,
                canonical_url=f"https://www.youtube.com/@{bare_handle}",
                handle=bare_handle,
            )
        return None

    def _video_parsed(self, video_id: str | None, original: str) -> ParsedUrl:
        assert video_id is not None
        return ParsedUrl(
            platform=self.platform,
            kind="post",
            original=original,
            canonical_url=f"https://www.youtube.com/watch?v={video_id}",
            resource_id=video_id,
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

        async def _run() -> ScrapeResult:
            return await self._fetch_profile_uncached(
                parsed, include_posts=include_posts, posts_limit=posts_limit
            )

        return await self.cached(key, self.settings.cache_ttl_profile_seconds, _run, refresh=refresh)

    async def _fetch_profile_uncached(
        self, parsed: ParsedUrl, *, include_posts: bool, posts_limit: int
    ) -> ScrapeResult:
        result = ScrapeResult(
            platform=self.platform,
            kind="profile",
            url=parsed.original,
            status=ResultStatus.ok,
            source=Source.embedded_json,
            confidence=Confidence.high,
        )

        base_url = parsed.canonical_url
        profile: Profile | None = None
        errors: list[str] = []

        try:
            response = await self.http.get(
                base_url,
                platform=self.platform.value,
                headers={"Accept": "text/html"},
            )
            html = response.text
            handle_from_url = _handle_from_path(response.final_url) or parsed.handle

            initial_data = find_json_after(html, "ytInitialData")
            if initial_data is None:
                raise ParseError("ytInitialData not found", platform="youtube", url=base_url)

            profile = self._parse_channel(initial_data, html, handle_from_url, response.final_url)
            if profile.metrics.followers is None:
                about = await self._fetch_about(base_url)
                if about is not None:
                    self._merge_about(profile, about)
                    result.add_warning("subscriber count came from the /about tab")
            result.profile = profile

            if include_posts and posts_limit > 0:
                result.posts = await self._fetch_videos(base_url, limit=posts_limit)
                if not result.posts:
                    result.add_warning("no videos found on the /videos tab")
        except (BlockedError, NotFoundError, ParseError, UpstreamError) as exc:
            errors.append(exc.message)
            self.log.warning("youtube_profile_failed", url=base_url, error=exc.message)

        if profile is None or profile.metrics.followers is None:
            fallback = await self._ytdlp_profile(parsed.canonical_url)
            if fallback is not None:
                if profile is None:
                    profile = fallback
                    result.profile = profile
                else:
                    if profile.metrics.followers is None:
                        profile.metrics.followers = fallback.metrics.followers
                    profile.metrics.posts = profile.metrics.posts or fallback.metrics.posts
                    profile.extra.setdefault("views_total", fallback.extra.get("views_total"))
                result.source = Source.ytdlp
                result.add_warning("profile metrics recovered via yt-dlp")

        if profile is None:
            api_profile = await self._api_profile(parsed)
            if api_profile is not None:
                profile = api_profile
                result.profile = profile
                result.source = Source.official_api
                result.confidence = Confidence.high

        if profile is None:
            result.status = ResultStatus.failed
            result.confidence = Confidence.low
            result.error = {
                "code": "parse_error",
                "message": "; ".join(errors) or "could not extract channel data",
            }
            return result

        self._finalize(result, profile)
        return result

    def _parse_channel(
        self, root: Any, html: str, handle: str | None, final_url: str
    ) -> Profile:
        title = _channel_title(root) or _strip_youtube_suffix(og_tags(html).get("title"))
        avatar = _channel_avatar(root) or og_tags(html).get("image")
        description = _channel_description(root) or og_tags(html).get("description")

        handle = (
            _channel_handle(root)
            or (handle if handle and not handle.startswith("UC") else None)
            or _handle_from_text(title)
        )

        subscribers = _find_metric(root, _SUBSCRIBER_RE)
        video_count = _find_metric(root, _VIDEO_COUNT_RE)

        channel_id = _channel_id(root) or dig(root, "metadata", "channelMetadataRenderer", "externalId")
        if channel_id is None:
            match = re.search(r'"channelId":"(UC[A-Za-z0-9_\-]{22})"', html)
            channel_id = match.group(1) if match else None

        if not handle and channel_id:
            handle = channel_id
        handle = handle or "unknown"

        extra: dict[str, Any] = {}
        views_total = _find_views_total(root)
        if views_total is not None:
            extra["views_total"] = views_total
        if description:
            extra["description"] = description

        return Profile(
            platform=self.platform,
            handle=handle,
            url=final_url or f"https://www.youtube.com/@{handle}",
            display_name=title,
            bio=description,
            avatar_url=avatar,
            verified=None,
            external_id=channel_id,
            metrics=ProfileMetrics(
                followers=subscribers,
                posts=video_count,
            ),
            extra=extra,
        )

    async def _fetch_about(self, base_url: str) -> dict[str, Any] | None:
        try:
            response = await self.http.get(
                f"{base_url.rstrip('/')}/about",
                platform=self.platform.value,
            )
        except (BlockedError, NotFoundError, UpstreamError):
            return None
        root = find_json_after(response.text, "ytInitialData")
        if root is None:
            return None
        about = find_key(root, "aboutChannelViewModel")
        return about if isinstance(about, dict) else None

    def _merge_about(self, profile: Profile, about: dict[str, Any]) -> None:
        subscriber_text = text_content(about.get("subscriberCountText"))
        if subscriber_text:
            profile.metrics.followers = parse_count(subscriber_text)
        video_text = text_content(about.get("videoCountText"))
        if video_text:
            profile.metrics.posts = profile.metrics.posts or parse_count(video_text)
        views_text = text_content(about.get("viewCountText"))
        if views_text:
            profile.extra.setdefault("views_total", parse_count(views_text))
        description = clean_text(text_content(about.get("description")))
        if description and not profile.bio:
            profile.bio = description
            profile.extra["description"] = description

    async def _fetch_videos(self, base_url: str, *, limit: int) -> list[Post]:
        try:
            response = await self.http.get(
                f"{base_url.rstrip('/')}/videos",
                platform=self.platform.value,
            )
        except (BlockedError, NotFoundError, UpstreamError) as exc:
            self.log.warning("youtube_videos_failed", url=base_url, error=exc.message)
            return []

        root = find_json_after(response.text, "ytInitialData")
        if root is None:
            return []

        posts: list[Post] = []
        seen: set[str] = set()

        for node in find_all(root, lambda item: isinstance(item, dict) and "videoRenderer" in item):
            renderer = node["videoRenderer"]
            post = _post_from_video_renderer(renderer)
            if post is None or post.url in seen:
                continue
            seen.add(post.url)
            posts.append(post)
            if len(posts) >= limit:
                return posts

        for node in find_all(
            root, lambda item: isinstance(item, dict) and "shortsLockupViewModel" in item
        ):
            post = _post_from_shorts_lockup(node["shortsLockupViewModel"])
            if post is None or post.url in seen:
                continue
            seen.add(post.url)
            posts.append(post)
            if len(posts) >= limit:
                return posts

        # Current channel grid markup (2025+) uses lockupViewModel instead of
        # videoRenderer. Try it before falling back to the RSS feed.
        for node in find_all(
            root, lambda item: isinstance(item, dict) and "lockupViewModel" in item
        ):
            post = _post_from_lockup(node["lockupViewModel"])
            if post is None or post.url in seen:
                continue
            seen.add(post.url)
            posts.append(post)
            if len(posts) >= limit:
                return posts

        if posts:
            return posts

        # Last resort: the channel RSS feed is public, tiny and stable.
        return await self._rss_videos(base_url, limit=limit)

    async def _rss_videos(self, base_url: str, *, limit: int) -> list[Post]:
        channel_id = base_url.rstrip("/").rsplit("/", 1)[-1]
        if not _CHANNEL_ID_RE.match(channel_id):
            return []
        try:
            response = await self.http.get(
                f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
                platform=self.platform.value,
                headers={"Accept": "application/xml"},
            )
        except (BlockedError, NotFoundError, UpstreamError):
            return []

        entries = re.findall(r"<entry>(.*?)</entry>", response.text, re.S)
        posts: list[Post] = []
        for entry in entries[:limit]:
            video_id = _first_group(r"<yt:videoId>(.*?)</yt:videoId>", entry)
            title = _first_group(r"<title>(.*?)</title>", entry)
            published = _first_group(r"<published>(.*?)</published>", entry)
            views = _first_group(r'<media:statistics views="(\d+)"', entry)
            if not video_id:
                continue
            posts.append(
                Post(
                    platform=self.platform,
                    id=video_id,
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    caption=clean_text(title),
                    published_at=_parse_iso_datetime(published),
                    media_type="video",
                    metrics=PostMetrics(views=int(views) if views else None),
                    extra={"via": "rss"},
                )
            )
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
        url = f"https://www.youtube.com/watch?v={video_id}"

        result = ScrapeResult(
            platform=self.platform,
            kind="post",
            url=parsed.original,
            status=ResultStatus.ok,
            source=Source.embedded_json,
            confidence=Confidence.high,
        )

        post: Post | None = None
        try:
            response = await self.http.get(
                url,
                platform=self.platform.value,
                headers={"Accept": "text/html"},
            )
            html = response.text
            player = find_json_after(html, "ytInitialPlayerResponse")
            initial_data = find_json_after(html, "ytInitialData")

            if player is not None:
                status = dig(player, "playabilityStatus", "status")
                if status in {"LOGIN_REQUIRED", "AGE_VERIFICATION_REQUIRED"}:
                    result.add_warning(f"video requires login ({status})")
                elif status == "ERROR":
                    raise NotFoundError("video unavailable", platform="youtube", url=url)

            post = self._parse_video(player, initial_data, html, video_id)
            result.post = post
        except (BlockedError, NotFoundError, ParseError, UpstreamError) as exc:
            self.log.warning("youtube_post_failed", url=url, error=exc.message)
            result.error = {"code": exc.code, "message": exc.message}

        if post is None or post.metrics.views is None:
            fallback = await self._ytdlp_post(url)
            if fallback is not None:
                result.post = fallback
                result.source = Source.ytdlp
                result.add_warning("video metrics recovered via yt-dlp")
                post = fallback

        if post is None:
            api_post = await self._api_post(video_id)
            if api_post is not None:
                result.post = api_post
                result.source = Source.official_api
                post = api_post

        if post is None:
            result.status = ResultStatus.failed
            result.confidence = Confidence.low
            result.error = result.error or {
                "code": "parse_error",
                "message": "could not extract video data",
            }
            return result

        if result.status == ResultStatus.ok:
            result.confidence = Confidence.high if result.source != Source.ytdlp else Confidence.medium
        return result

    def _parse_video(
        self, player: Any, initial_data: Any, html: str, video_id: str
    ) -> Post | None:
        details = dig(player, "videoDetails")
        microformat = dig(player, "microformat", "playerMicroformatRenderer")

        if not isinstance(details, dict):
            details = find_key(player, "videoDetails") if player is not None else None
        if not isinstance(details, dict):
            details = {}

        if not details and initial_data is None:
            return None

        title = details.get("title") or _watch_title(initial_data) or og_tags(html).get("title")
        views = parse_count(details.get("viewCount"))
        if views is None:
            views = _find_metric(initial_data, _VIEW_RE) if initial_data is not None else None
        if views is None:
            views = _find_metric(html, _VIEW_RE)

        likes = _find_metric(initial_data, _LIKES_RE) if initial_data is not None else None
        if likes is None:
            likes = _find_metric(html, _LIKES_RE)

        comments = _comment_count(initial_data) if initial_data is not None else None

        published_at = None
        if isinstance(microformat, dict):
            published_at = _parse_iso_datetime(
                microformat.get("publishDate") or microformat.get("uploadDate")
            )

        duration = parse_duration(details.get("lengthSeconds"))

        thumbnails = dig(details, "thumbnail", "thumbnails")
        thumbnail = thumbnails[-1].get("url") if isinstance(thumbnails, list) and thumbnails else None

        return Post(
            platform=self.platform,
            id=video_id,
            url=f"https://www.youtube.com/watch?v={video_id}",
            caption=clean_text(title),
            thumbnail_url=thumbnail or og_tags(html).get("image"),
            published_at=published_at,
            duration_seconds=duration,
            media_type="short" if "/shorts/" in html[:5000] else "video",
            metrics=PostMetrics(views=views, likes=likes, comments=comments),
            extra={
                "channel": details.get("author"),
                "channel_id": details.get("channelId"),
                "description": clean_text(details.get("shortDescription")),
                "is_live": bool(details.get("isLiveContent")),
            },
        )

    # ------------------------------------------------------------------ fallbacks
    async def _ytdlp_profile(self, url: str) -> Profile | None:
        data = await self._run_ytdlp([url, "--flat-playlist", "--playlist-end", "1"])
        if not data:
            return None
        return Profile(
            platform=self.platform,
            handle=data.get("uploader_id") or data.get("channel_id") or "unknown",
            url=data.get("channel_url") or url,
            display_name=data.get("channel") or data.get("uploader"),
            bio=clean_text(data.get("description")),
            avatar_url=None,
            external_id=data.get("channel_id"),
            metrics=ProfileMetrics(
                followers=data.get("channel_follower_count"),
                posts=None,
            ),
            extra={"via": "ytdlp"},
        )

    async def _ytdlp_post(self, url: str) -> Post | None:
        data = await self._run_ytdlp([url])
        if not data:
            return None
        return Post(
            platform=self.platform,
            id=data.get("id"),
            url=data.get("webpage_url") or url,
            caption=clean_text(data.get("title")),
            thumbnail_url=data.get("thumbnail"),
            published_at=_parse_upload_date(data.get("upload_date")),
            duration_seconds=parse_duration(data.get("duration")),
            media_type="video",
            metrics=PostMetrics(
                views=data.get("view_count"),
                likes=data.get("like_count"),
                comments=data.get("comment_count"),
            ),
            extra={
                "channel": data.get("uploader"),
                "channel_id": data.get("channel_id"),
                "description": clean_text(data.get("description")),
                "is_live": data.get("is_live"),
                "via": "ytdlp",
            },
        )

    async def _run_ytdlp(self, args: list[str]) -> dict[str, Any] | None:
        if not self.settings.enable_ytdlp_fallback:
            return None
        binary = shutil.which("yt-dlp")
        if binary is None:
            return None
        command = [
            binary,
            "--dump-single-json",
            "--skip-download",
            "--no-warnings",
            "--no-playlist" if "--flat-playlist" not in args else "--flat-playlist",
            *args,
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=45)
        except (TimeoutError, OSError) as exc:
            self.log.warning("ytdlp_failed", error=str(exc))
            return None

        if process.returncode != 0 or not stdout:
            return None
        try:
            return json.loads(stdout.decode("utf-8", errors="ignore"))
        except json.JSONDecodeError:
            return None

    async def _api_profile(self, parsed: ParsedUrl) -> Profile | None:
        key = self.settings.youtube_api_key
        if not key:
            return None
        params: dict[str, str] = {"part": "statistics,snippet", "key": key}
        if parsed.handle and parsed.handle.startswith("UC"):
            params["id"] = parsed.handle
        else:
            params["forHandle"] = f"@{parsed.handle}" if parsed.handle else ""
        try:
            response = await self.http.get(
                "https://www.googleapis.com/youtube/v3/channels",
                platform=self.platform.value,
                params=params,
                headers={"Accept": "application/json"},
            )
        except (BlockedError, NotFoundError, UpstreamError):
            return None

        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError:
            return None
        items = payload.get("items") or []
        if not items:
            return None
        item = items[0]
        snippet = item.get("snippet", {})
        statistics = item.get("statistics", {})
        return Profile(
            platform=self.platform,
            handle=snippet.get("customUrl") or item.get("id") or "unknown",
            url=f"https://www.youtube.com/channel/{item.get('id')}",
            display_name=snippet.get("title"),
            bio=clean_text(snippet.get("description")),
            avatar_url=dig(snippet, "thumbnails", "high", "url"),
            external_id=item.get("id"),
            metrics=ProfileMetrics(
                followers=parse_count(statistics.get("subscriberCount")),
                posts=parse_count(statistics.get("videoCount")),
            ),
            extra={"views_total": parse_count(statistics.get("viewCount"))},
        )

    async def _api_post(self, video_id: str) -> Post | None:
        key = self.settings.youtube_api_key
        if not key:
            return None
        try:
            response = await self.http.get(
                "https://www.googleapis.com/youtube/v3/videos",
                platform=self.platform.value,
                params={
                    "part": "statistics,snippet,contentDetails",
                    "id": video_id,
                    "key": key,
                },
                headers={"Accept": "application/json"},
            )
        except (BlockedError, NotFoundError, UpstreamError):
            return None

        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError:
            return None
        items = payload.get("items") or []
        if not items:
            return None
        item = items[0]
        snippet = item.get("snippet", {})
        statistics = item.get("statistics", {})
        return Post(
            platform=self.platform,
            id=video_id,
            url=f"https://www.youtube.com/watch?v={video_id}",
            caption=clean_text(snippet.get("title")),
            thumbnail_url=dig(snippet, "thumbnails", "high", "url"),
            published_at=_parse_iso_datetime(snippet.get("publishedAt")),
            duration_seconds=parse_duration(dig(item, "contentDetails", "duration")),
            media_type="video",
            metrics=PostMetrics(
                views=parse_count(statistics.get("viewCount")),
                likes=parse_count(statistics.get("likeCount")),
                comments=parse_count(statistics.get("commentCount")),
            ),
            extra={"channel": snippet.get("channelTitle"), "via": "official_api"},
        )

    # ------------------------------------------------------------------ finalize
    def _finalize(self, result: ScrapeResult, profile: Profile) -> None:
        from services.normalize import finalize

        finalize(
            result,
            required=["followers", "posts"] if result.posts else ["followers"],
            confidence=Confidence.high
            if result.source in {Source.embedded_json, Source.official_api}
            else Confidence.medium,
        )
        if profile.metrics.posts is None and result.posts:
            profile.metrics.posts = len(result.posts)
            result.add_warning("total video count unavailable; posts reflects the sample")


# ---------------------------------------------------------------------- helpers


def _valid_video_id(video_id: str | None) -> bool:
    return isinstance(video_id, str) and bool(re.fullmatch(r"[A-Za-z0-9_\-]{11}", video_id))


def _handle_from_path(url: str) -> str | None:
    path = urlparse(url).path.strip("/")
    for segment in path.split("/"):
        if segment.startswith("@"):
            return segment[1:]
    return None


def _handle_from_text(text: str | None) -> str | None:
    if not text:
        return None
    match = _HANDLE_RE.search(text)
    return match.group(1) if match else None


def _strip_youtube_suffix(text: str | None) -> str | None:
    if not text:
        return None
    return re.sub(r"\s*-\s*YouTube$", "", text).strip() or None


def _channel_title(root: Any) -> str | None:
    header = find_key(root, "pageHeaderViewModel")
    if isinstance(header, dict):
        title = text_content(dig(header, "title", "dynamicTextViewModel", "text"))
        if title:
            return clean_text(title)
    legacy = find_key(root, "c4TabbedHeaderRenderer")
    if isinstance(legacy, dict):
        title = text_content(legacy.get("title"))
        if title:
            return clean_text(title)
    metadata = find_key(root, "channelMetadataRenderer")
    if isinstance(metadata, dict):
        return clean_text(metadata.get("title"))
    return None


def _channel_handle(root: Any) -> str | None:
    metadata = find_key(root, "channelMetadataRenderer")
    if isinstance(metadata, dict):
        vanity = metadata.get("vanityChannelUrl") or ""
        handle = _handle_from_text(vanity)
        if handle:
            return handle
    header = find_key(root, "pageHeaderViewModel")
    if isinstance(header, dict):
        rows = dig(header, "metadata", "contentMetadataViewModel", "metadataRows")
        for row in rows or []:
            for part in dig(row, "metadataParts") or []:
                text = text_content(part.get("text"))
                handle = _handle_from_text(text)
                if handle:
                    return handle
    return None


def _channel_avatar(root: Any) -> str | None:
    header = find_key(root, "pageHeaderViewModel")
    if isinstance(header, dict):
        sources = dig(
            header, "image", "decoratedAvatarViewModel", "avatar", "avatarViewModel", "image", "sources"
        )
        if isinstance(sources, list) and sources:
            return sources[-1].get("url")
    legacy = find_key(root, "c4TabbedHeaderRenderer")
    if isinstance(legacy, dict):
        sources = dig(legacy, "avatar", "thumbnails")
        if isinstance(sources, list) and sources:
            return sources[-1].get("url")
    return None


def _channel_description(root: Any) -> str | None:
    header = find_key(root, "pageHeaderViewModel")
    if isinstance(header, dict):
        description = dig(
            header, "description", "descriptionPreviewViewModel", "description", "content"
        )
        if description:
            return clean_text(description)
    metadata = find_key(root, "channelMetadataRenderer")
    if isinstance(metadata, dict):
        return clean_text(metadata.get("description"))
    return None


def _channel_id(root: Any) -> str | None:
    metadata = find_key(root, "channelMetadataRenderer")
    if isinstance(metadata, dict):
        external = metadata.get("externalId")
        if external:
            return external
    browse_id = dig(root, "metadata", "channelMetadataRenderer", "externalId")
    if isinstance(browse_id, str):
        return browse_id
    for node in find_all(root, lambda item: isinstance(item, dict) and "browseId" in item):
        candidate = node.get("browseId")
        if isinstance(candidate, str) and _CHANNEL_ID_RE.match(candidate):
            return candidate
    return None


def _find_metric(root: Any, pattern: re.Pattern[str]) -> int | None:
    """Find the first text matching a "N subscribers"-style pattern."""
    if isinstance(root, str):
        match = pattern.search(root)
        return parse_count(match.group(1)) if match else None
    for node in _iter_texts(root):
        match = pattern.search(node)
        if match:
            return parse_count(match.group(1))
    return None


def _find_metric_text(root: Any, pattern: re.Pattern[str]) -> str | None:
    if isinstance(root, str):
        match = pattern.search(root)
        return match.group(0) if match else None
    for node in _iter_texts(root):
        match = pattern.search(node)
        if match:
            return match.group(0)
    return None


def _find_views_total(root: Any) -> int | None:
    about = find_key(root, "aboutChannelViewModel")
    if isinstance(about, dict):
        text = text_content(about.get("viewCountText"))
        if text:
            match = _VIEW_RE.search(text)
            if match:
                return parse_count(match.group(1))
    return None


def _iter_texts(root: Any) -> list[str]:
    """Flatten every human-readable string inside a YouTube JSON tree."""
    from services.jsonblob import walk

    texts: list[str] = []
    for node in walk(root):
        if isinstance(node, dict):
            for key in ("simpleText", "content"):
                value = node.get(key)
                if isinstance(value, str) and value:
                    texts.append(value)
            runs = node.get("runs")
            if isinstance(runs, list):
                joined = "".join(
                    str(run.get("text", "")) for run in runs if isinstance(run, dict)
                )
                if joined:
                    texts.append(joined)
    return texts


def _comment_count(root: Any) -> int | None:
    entry = find_key(root, "commentsEntryPointHeaderRenderer")
    if isinstance(entry, dict):
        count = text_content(entry.get("commentCount"))
        if count:
            parsed = parse_count(count)
            if parsed is not None:
                return parsed
    for node in _iter_texts(root):
        match = re.search(r"([\d.,]+\s*[KMB]?)\s*(?:comments?|comentários?)", node, re.I)
        if match:
            return parse_count(match.group(1))
    return None


def _watch_title(root: Any) -> str | None:
    primary = find_key(root, "videoPrimaryInfoRenderer")
    if isinstance(primary, dict):
        return text_content(primary.get("title"))
    return None


def _post_from_video_renderer(renderer: Any) -> Post | None:
    if not isinstance(renderer, dict):
        return None
    video_id = renderer.get("videoId")
    if not _valid_video_id(video_id):
        return None
    assert isinstance(video_id, str)
    title = text_content(renderer.get("title"))
    views_text = text_content(renderer.get("viewCountText"))
    published_text = text_content(renderer.get("publishedTimeText"))
    length_text = text_content(renderer.get("lengthText"))
    thumbnails = dig(renderer, "thumbnail", "thumbnails")
    thumbnail = thumbnails[-1].get("url") if isinstance(thumbnails, list) and thumbnails else None

    views = None
    if views_text:
        match = _VIEW_RE.search(views_text)
        views = parse_count(match.group(1)) if match else parse_count(views_text)

    return Post(
        platform=Platform.youtube,
        id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        caption=clean_text(title),
        thumbnail_url=thumbnail,
        published_at=parse_relative_date(published_text),
        duration_seconds=parse_duration(length_text),
        media_type="video",
        metrics=PostMetrics(views=views),
        extra={
            "published_text": published_text,
            "views_text": views_text,
            "via": "channel_videos_tab",
        },
    )


def _post_from_shorts_lockup(lockup: Any) -> Post | None:
    if not isinstance(lockup, dict):
        return None
    video_id = dig(lockup, "onTap", "innertubeCommand", "reelWatchEndpoint", "videoId")
    if not video_id:
        video_id = dig(lockup, "entityId")
    if not _valid_video_id(video_id):
        return None
    assert isinstance(video_id, str)
    title = dig(lockup, "overlayMetadata", "primaryText", "content")
    views_text = dig(lockup, "overlayMetadata", "secondaryText", "content")
    sources = dig(lockup, "thumbnail", "sources")
    thumbnail = sources[-1].get("url") if isinstance(sources, list) and sources else None
    views = None
    if views_text:
        match = _VIEW_RE.search(views_text)
        views = parse_count(match.group(1)) if match else parse_count(views_text)
    return Post(
        platform=Platform.youtube,
        id=video_id,
        url=f"https://www.youtube.com/shorts/{video_id}",
        caption=clean_text(title),
        thumbnail_url=thumbnail,
        media_type="short",
        metrics=PostMetrics(views=views),
        extra={"views_text": views_text, "via": "shorts_lockup"},
    )


def _post_from_lockup(lockup: Any) -> Post | None:
    """Parse the ``lockupViewModel`` used by the 2025+ channel grid markup."""
    if not isinstance(lockup, dict):
        return None
    video_id = lockup.get("contentId")
    if not _valid_video_id(video_id):
        return None
    assert isinstance(video_id, str)

    content_type = lockup.get("contentType") or ""
    is_short = isinstance(content_type, str) and content_type.endswith("_SHORT")

    title = dig(lockup, "metadata", "lockupMetadataViewModel", "title", "content")
    rows = dig(
        lockup,
        "metadata",
        "lockupMetadataViewModel",
        "metadata",
        "contentMetadataViewModel",
        "metadataRows",
    )

    views_text: str | None = None
    published_text: str | None = None
    plain_parts: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        parts = row.get("metadataParts")
        for part in parts if isinstance(parts, list) else []:
            if not isinstance(part, dict):
                continue
            text = (text_content(part.get("text")) or text_content(part) or "").strip()
            if not text:
                continue
            plain_parts.append(text)
            icon = dig(part, "leadingIcon", "name")
            if icon == "PLAY_ARROW_OUTLINED" and views_text is None:
                views_text = text
            elif published_text is None and _REL_DATE_HINT_RE.search(text):
                published_text = text

    if views_text is None and plain_parts:
        views_text = plain_parts[0]
    if published_text is None and len(plain_parts) > 1:
        published_text = plain_parts[1]

    duration_text: str | None = None
    overlays = dig(lockup, "contentImage", "thumbnailViewModel", "overlays")
    for overlay in overlays if isinstance(overlays, list) else []:
        badges = dig(overlay, "thumbnailBottomOverlayViewModel", "badges")
        for badge in badges if isinstance(badges, list) else []:
            candidate = dig(badge, "thumbnailBadgeViewModel", "text")
            if isinstance(candidate, str) and _DURATION_RE.match(candidate.strip()):
                duration_text = candidate.strip()
                break
        if duration_text:
            break

    sources = dig(lockup, "contentImage", "thumbnailViewModel", "image", "sources")
    thumbnail = None
    if isinstance(sources, list) and sources:
        last = sources[-1]
        thumbnail = last.get("url") if isinstance(last, dict) else None

    return Post(
        platform=Platform.youtube,
        id=video_id,
        url=(
            f"https://www.youtube.com/shorts/{video_id}"
            if is_short
            else f"https://www.youtube.com/watch?v={video_id}"
        ),
        caption=clean_text(title),
        thumbnail_url=thumbnail,
        published_at=parse_relative_date(published_text),
        duration_seconds=parse_duration(duration_text),
        media_type="short" if is_short else "video",
        metrics=PostMetrics(views=parse_count(views_text) if views_text else None),
        extra={
            "views_text": views_text,
            "published_text": published_text,
            "content_type": content_type or None,
            "via": "lockup",
        },
    )


def _first_group(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, re.S)
    return match.group(1) if match else None


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    from dateutil import parser as date_parser

    try:
        parsed = date_parser.isoparse(value)
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _parse_upload_date(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) != 8:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=UTC)
    except ValueError:
        return None
