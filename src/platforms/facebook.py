"""Facebook adapter.

Facebook has no anonymous JSON surface and aggressively redirects anonymous
traffic to a login wall. The adapter therefore:

1. tries ``mbasic.facebook.com`` (server-rendered, no JavaScript, lightest
   possible request);
2. falls back to ``www.facebook.com`` and reads the ``og:`` tags;
3. reports ``partial`` with explicit warnings when only some counters are
   visible, instead of failing the whole request.

Cookie support is strongly recommended: drop a Netscape ``cookies.txt`` at
``cookies/facebook.txt``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from core.errors import AuthRequiredError, BlockedError, NotFoundError, UpstreamError
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
from services.jsonblob import og_tags

_RESERVED_PATHS = {
    "watch",
    "reel",
    "reels",
    "stories",
    "groups",
    "events",
    "marketplace",
    "gaming",
    "login",
    "login.php",
    "help",
    "policies",
    "privacy",
    "settings",
    "share",
    "sharer",
    "photo",
    "permalink.php",
    "story.php",
    "profile.php",
}

_POST_PATH_RE = re.compile(r"/(?:posts|videos|reel|photos)/([A-Za-z0-9._\-]{5,})")

_LIKES_RE = re.compile(r"([\d.,]+\s*[KMB]?)\s*(?:likes?|curtidas?)", re.I)
_FOLLOWERS_RE = re.compile(r"([\d.,]+\s*[KMB]?)\s*(?:followers?|seguidores?)", re.I)
_TALKING_RE = re.compile(
    r"([\d.,]+\s*[KMB]?)\s*(?:talking about this|falando sobre (?:isso|isto)|pessoas falando)", re.I
)
_COMMENTS_RE = re.compile(r"([\d.,]+\s*[KMB]?)\s*(?:comments?|coment[áa]rios?)", re.I)
_SHARES_RE = re.compile(r"([\d.,]+\s*[KMB]?)\s*(?:shares?|compartilhamentos?)", re.I)
_VIEWS_RE = re.compile(r"([\d.,]+\s*[KMB]?)\s*(?:views?|visualiza[çc][õo]es)", re.I)


class FacebookAdapter(BaseAdapter):
    platform = Platform.facebook
    capabilities = PlatformCapabilities(
        platform=Platform.facebook,
        supports_profile=True,
        supports_posts=False,
        supports_single_post=True,
        html_reliability="low",
        profile_fields=["followers", "likes_total", "display_name", "bio", "avatar_url"],
        post_fields=["likes", "comments", "shares", "views"],
        requires_cookies_for=["profile metrics", "post metrics", "posts list"],
        notes=(
            "Anonymous HTML exposes little beyond og: tags, and Facebook often "
            "serves a login wall. Treat results as indicative, not authoritative."
        ),
    )

    # ------------------------------------------------------------------ detection
    def detect(self, url: str) -> ParsedUrl | None:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        host = (parsed.netloc or "").lower().removeprefix("www.").removeprefix("m.").removeprefix(
            "web."
        )
        if host not in {"facebook.com", "fb.com", "fb.watch", "mbasic.facebook.com"}:
            return None

        if host == "fb.watch":
            code = parsed.path.strip("/")
            if not code:
                return None
            return ParsedUrl(
                platform=self.platform,
                kind="post",
                original=url,
                canonical_url=url,
                resource_id=code,
                extra={"short_code": code},
            )

        path = parsed.path.strip("/")
        if not path:
            return None

        if path.startswith("permalink.php") or path.startswith("story.php"):
            query = parse_qs(parsed.query)
            story_id = _first_param(query, "story_fbid", "fbid")
            page_id = _first_param(query, "id")
            if story_id:
                return ParsedUrl(
                    platform=self.platform,
                    kind="post",
                    original=url,
                    canonical_url=f"https://www.facebook.com/{page_id or 'story'}/posts/{story_id}",
                    handle=page_id,
                    resource_id=story_id,
                )
            return None

        if path.startswith("watch"):
            query = parse_qs(parsed.query)
            video_id = _first_param(query, "v")
            if video_id:
                return ParsedUrl(
                    platform=self.platform,
                    kind="post",
                    original=url,
                    canonical_url=f"https://www.facebook.com/watch/?v={video_id}",
                    resource_id=video_id,
                )
            return None

        match = _POST_PATH_RE.search(f"/{path}")
        if match:
            handle = _first_segment(path)
            return ParsedUrl(
                platform=self.platform,
                kind="post",
                original=url,
                canonical_url=f"https://www.facebook.com/{handle}/posts/{match.group(1)}",
                handle=handle,
                resource_id=match.group(1),
            )

        handle = _first_segment(path)
        if not handle or handle.lower() in _RESERVED_PATHS:
            return None
        handle = self.clean_handle(handle)
        if not handle:
            return None
        return ParsedUrl(
            platform=self.platform,
            kind="profile",
            original=url,
            canonical_url=f"https://www.facebook.com/{handle}",
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
        self, parsed: ParsedUrl, *, include_posts: bool = True, posts_limit: int = 12
    ) -> ScrapeResult:
        assert parsed.handle is not None
        handle = parsed.handle

        result = ScrapeResult(
            platform=self.platform,
            kind="profile",
            url=parsed.original,
            status=ResultStatus.ok,
            source=Source.html,
            confidence=Confidence.low,
        )

        errors: list[str] = []
        profile: Profile | None = None

        for base in ("https://mbasic.facebook.com", "https://www.facebook.com"):
            url = f"{base}/{handle}"
            try:
                response = await self.http.get(
                    url,
                    platform=self.platform.value,
                    headers={
                        "Accept": "text/html,application/xhtml+xml",
                        "Referer": "https://www.facebook.com/",
                    },
                )
            except (BlockedError, NotFoundError, UpstreamError) as exc:
                errors.append(f"{base}: {exc.message}")
                continue

            html = response.text
            if _is_login_wall(html):
                errors.append(f"{base}: login wall")
                continue

            profile = _profile_from_html(html, handle)
            if profile is not None:
                result.source = Source.html
                if base.startswith("https://mbasic"):
                    result.add_warning("data read from mbasic.facebook.com")
                break

        if profile is None:
            has_cookies = self.http.has_cookies(self.platform.value)
            result.status = ResultStatus.failed
            result.confidence = Confidence.low
            message = (
                "Facebook served a login wall for every anonymous surface. "
                "Provide cookies (COOKIES_FACEBOOK_FILE) to unlock profile data."
            )
            if has_cookies:
                message = "Facebook blocked the request even with cookies; try a proxy or refresh the cookies."
            result.error = {
                "code": AuthRequiredError.code,
                "message": message,
                "detail": "; ".join(errors) or None,
            }
            return result

        result.profile = profile

        if include_posts:
            result.add_warning("Facebook does not expose post lists without authentication")

        self._finalize(result)
        return result

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
        result = ScrapeResult(
            platform=self.platform,
            kind="post",
            url=parsed.original,
            status=ResultStatus.ok,
            source=Source.html,
            confidence=Confidence.low,
        )

        target = parsed.extra.get("short_code")
        url = parsed.canonical_url
        if target:
            url = f"https://fb.watch/{target}/"

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
        if _is_login_wall(html):
            result.status = ResultStatus.failed
            result.confidence = Confidence.low
            result.error = {
                "code": AuthRequiredError.code,
                "message": "Facebook served a login wall for this post.",
            }
            return result

        post = _post_from_html(html, parsed)
        if post is None:
            result.status = ResultStatus.failed
            result.confidence = Confidence.low
            result.error = {"code": "parse_error", "message": "post payload not found"}
            return result

        result.post = post
        if post.metrics.likes is None and post.metrics.comments is None:
            result.add_warning("Facebook hid engagement counters for this post")
        return result

    # ------------------------------------------------------------------ finalize
    def _finalize(self, result: ScrapeResult) -> None:
        from services.normalize import finalize

        finalize(result, required=["followers"], confidence=Confidence.low)


# ---------------------------------------------------------------------- helpers


def _first_param(query: dict[str, list[str]], *keys: str) -> str | None:
    """Return the first present, non-empty value among ``keys``."""
    for key in keys:
        values = query.get(key)
        if values and values[0]:
            return values[0]
    return None


def _first_segment(path: str) -> str | None:
    for segment in path.split("/"):
        if segment:
            return segment.split("?")[0]
    return None


def _is_login_wall(html: str) -> bool:
    lowered = html[:6000].lower()
    markers = (
        "you must log in to continue",
        "log into facebook",
        "log in to facebook",
        "entre no facebook",
        "faça login para continuar",
        'id="login_form"',
        "checkpoint",
    )
    if "og:title" in lowered and "facebook" in lowered and "login" not in lowered:
        return False
    return any(marker in lowered for marker in markers)


# Strings Facebook returns instead of real page metadata when it decides the
# visitor must sign in. They must never leak into ``display_name``/``bio``.
_PLACEHOLDER_TEXT = {
    "entre ou cadastre-se para visualizar",
    "log in or sign up to view",
    "inicia sesión o regístrate para verlo",
    "facebook",
    "veja posts, fotos e muito mais no facebook.",
    "see posts, photos and more on facebook.",
}


def _is_placeholder(text: str | None) -> bool:
    if text is None:
        return False
    return text.strip().lower() in _PLACEHOLDER_TEXT


def _profile_from_html(html: str, handle: str) -> Profile | None:
    tags = og_tags(html)
    description = tags.get("description") or ""
    title = tags.get("title") or ""

    if _is_placeholder(title):
        title = ""
    if _is_placeholder(description):
        description = ""

    likes = _metric(description, _LIKES_RE)
    followers = _metric(description, _FOLLOWERS_RE)
    talking = _metric(description, _TALKING_RE)

    if likes is None:
        likes = _metric(html, _LIKES_RE)
    if followers is None:
        followers = _metric(html, _FOLLOWERS_RE)

    if likes is None and followers is None and not title:
        return None

    display_name = clean_text(re.sub(r"\s*\|\s*Facebook\s*$", "", title)) or None

    extra: dict[str, Any] = {"via": "og_tags"}
    if talking is not None:
        extra["talking_about"] = talking

    return Profile(
        platform=Platform.facebook,
        handle=handle,
        url=f"https://www.facebook.com/{handle}",
        display_name=display_name,
        bio=clean_text(description) or None,
        avatar_url=tags.get("image"),
        verified=None,
        metrics=ProfileMetrics(
            followers=followers if followers is not None else likes,
            likes_total=likes,
        ),
        extra=extra,
    )


def _post_from_html(html: str, parsed: ParsedUrl) -> Post | None:
    tags = og_tags(html)
    description = tags.get("description") or ""
    title = tags.get("title") or ""

    likes = _metric(description, _LIKES_RE)
    comments = _metric(description, _COMMENTS_RE)
    shares = _metric(description, _SHARES_RE)
    views = _metric(description, _VIEWS_RE)

    if likes is None and comments is None and shares is None and views is None and not title:
        return None

    published_at = _published_from_html(html)

    post_id = parsed.resource_id or "unknown"
    return Post(
        platform=Platform.facebook,
        id=post_id,
        url=parsed.canonical_url,
        caption=clean_text(title) or clean_text(description),
        thumbnail_url=tags.get("image"),
        published_at=published_at,
        media_type="unknown",
        metrics=PostMetrics(likes=likes, comments=comments, shares=shares, views=views),
        extra={"author": parsed.handle, "via": "og_tags"},
    )


def _published_from_html(html: str) -> datetime | None:
    match = re.search(r'"publish_time":(\d{10})', html) or re.search(
        r'property="article:published_time"\s+content="([^"]+)"', html
    )
    if not match:
        return None
    raw = match.group(1)
    if raw.isdigit():
        return datetime.fromtimestamp(int(raw), tz=UTC)
    try:
        from dateutil import parser as date_parser

        parsed = date_parser.isoparse(raw)
    except (ValueError, OverflowError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _metric(text: str, pattern: re.Pattern[str]) -> int | None:
    if not text:
        return None
    match = pattern.search(text)
    return parse_count(match.group(1)) if match else None
