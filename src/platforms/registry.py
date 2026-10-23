"""Adapter registry: maps an arbitrary URL to the platform that can handle it."""

from __future__ import annotations

from collections.abc import Iterable

from core.errors import UnsupportedUrlError
from models.schemas import Platform
from platforms.base import AdapterContext, BaseAdapter, ParsedUrl
from platforms.facebook import FacebookAdapter
from platforms.instagram import InstagramAdapter
from platforms.tiktok import TikTokAdapter
from platforms.youtube import YouTubeAdapter


class AdapterRegistry:
    """Resolves URLs to adapters and exposes platform capabilities."""

    def __init__(self, adapters: Iterable[BaseAdapter]) -> None:
        self._adapters: dict[Platform, BaseAdapter] = {a.platform: a for a in adapters}

    @classmethod
    def default(cls, context: AdapterContext) -> AdapterRegistry:
        return cls(
            [
                YouTubeAdapter(context),
                TikTokAdapter(context),
                InstagramAdapter(context),
                FacebookAdapter(context),
            ]
        )

    # ------------------------------------------------------------------ lookup
    @property
    def adapters(self) -> dict[Platform, BaseAdapter]:
        return dict(self._adapters)

    def platforms(self) -> list[Platform]:
        return sorted(self._adapters, key=lambda p: p.value)

    def get(self, platform: Platform | str) -> BaseAdapter:
        key = Platform(platform) if isinstance(platform, str) else platform
        adapter = self._adapters.get(key)
        if adapter is None:
            raise UnsupportedUrlError(f"platform not enabled: {key}")
        return adapter

    def detect(self, url: str) -> ParsedUrl:
        for adapter in self._adapters.values():
            parsed = adapter.detect(url)
            if parsed is not None:
                return parsed
        raise UnsupportedUrlError(
            "URL does not match a supported platform",
            detail={"url": url},
        )

    def detect_or_none(self, url: str) -> ParsedUrl | None:
        for adapter in self._adapters.values():
            parsed = adapter.detect(url)
            if parsed is not None:
                return parsed
        return None

    def capabilities(self) -> list:
        return [adapter.capabilities for adapter in self._adapters.values()]


__all__ = [
    "AdapterRegistry",
    "FacebookAdapter",
    "InstagramAdapter",
    "TikTokAdapter",
    "YouTubeAdapter",
]
