"""URL resolution helpers shared by the route modules."""

from __future__ import annotations

from models.schemas import Platform
from platforms.base import BaseAdapter, ParsedUrl

_HANDLE_TEMPLATES: dict[Platform, str] = {
    Platform.youtube: "https://www.youtube.com/@{handle}",
    Platform.tiktok: "https://www.tiktok.com/@{handle}",
    Platform.instagram: "https://www.instagram.com/{handle}/",
    Platform.facebook: "https://www.facebook.com/{handle}",
}


def resolve_profile_url(
    adapter: BaseAdapter, platform: Platform, handle_or_url: str
) -> ParsedUrl | None:
    """Accept either a full URL or a bare handle and return a profile ParsedUrl."""
    parsed = adapter.detect(handle_or_url)
    if parsed is not None and parsed.kind == "profile":
        return parsed

    clean = adapter.clean_handle(handle_or_url)
    if clean is None:
        return None

    template = _HANDLE_TEMPLATES.get(platform)
    if template is None:
        return None

    candidate = adapter.detect(template.format(handle=clean))
    if candidate is not None and candidate.kind == "profile":
        return candidate

    return ParsedUrl(
        platform=platform,
        kind="profile",
        original=handle_or_url,
        canonical_url=template.format(handle=clean),
        handle=clean,
    )


def resolve_post_url(adapter: BaseAdapter, url: str) -> ParsedUrl | None:
    parsed = adapter.detect(url)
    if parsed is not None and parsed.kind == "post":
        return parsed
    return None
