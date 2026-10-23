"""URL detection tests for every adapter."""

from __future__ import annotations

import pytest

from core.errors import UnsupportedUrlError
from platforms.registry import AdapterRegistry


@pytest.mark.parametrize(
    ("url", "platform", "kind", "handle", "resource_id"),
    [
        ("https://www.youtube.com/@mkbhd", "youtube", "profile", "mkbhd", None),
        ("https://youtube.com/@mkbhd/videos", "youtube", "profile", "mkbhd", None),
        ("https://www.youtube.com/c/mkbhd", "youtube", "profile", "mkbhd", None),
        (
            "https://www.youtube.com/channel/UCBJycsmduvYEL83R_U4JriQ",
            "youtube",
            "profile",
            "UCBJycsmduvYEL83R_U4JriQ",
            None,
        ),
        ("https://youtu.be/9GSDvO0LFFE", "youtube", "post", None, "9GSDvO0LFFE"),
        ("https://www.youtube.com/watch?v=9GSDvO0LFFE", "youtube", "post", None, "9GSDvO0LFFE"),
        ("https://www.youtube.com/shorts/abc12345678", "youtube", "post", None, "abc12345678"),
        ("https://www.tiktok.com/@khaby.lame", "tiktok", "profile", "khaby.lame", None),
        ("https://www.tiktok.com/@khaby.lame?lang=en", "tiktok", "profile", "khaby.lame", None),
        (
            "https://www.tiktok.com/@khaby.lame/video/7300000000000000000",
            "tiktok",
            "post",
            "khaby.lame",
            "7300000000000000000",
        ),
        ("https://www.instagram.com/nasa/", "instagram", "profile", "nasa", None),
        ("https://instagram.com/nasa", "instagram", "profile", "nasa", None),
        ("https://www.instagram.com/p/CxYzAbCdEfG/", "instagram", "post", None, "CxYzAbCdEfG"),
        ("https://www.instagram.com/reel/CxYzAbCdEfG/", "instagram", "post", None, "CxYzAbCdEfG"),
        ("https://www.facebook.com/nasa", "facebook", "profile", "nasa", None),
        ("https://www.facebook.com/nasa/posts/1234567890", "facebook", "post", "nasa", "1234567890"),
        ("https://www.facebook.com/story.php?story_fbid=111&id=222", "facebook", "post", "222", "111"),
        ("https://www.facebook.com/watch/?v=333444", "facebook", "post", None, "333444"),
    ],
)
def test_detect(
    registry: AdapterRegistry,
    url: str,
    platform: str,
    kind: str,
    handle: str | None,
    resource_id: str | None,
) -> None:
    parsed = registry.detect(url)
    assert parsed.platform.value == platform
    assert parsed.kind == kind
    assert parsed.handle == handle
    assert parsed.resource_id == resource_id


def test_detect_rejects_unknown_host(registry: AdapterRegistry) -> None:
    with pytest.raises(UnsupportedUrlError):
        registry.detect("https://example.com/nasa")


def test_detect_rejects_garbage(registry: AdapterRegistry) -> None:
    with pytest.raises(UnsupportedUrlError):
        registry.detect("not a url at all")


def test_registry_exposes_all_four_platforms(registry: AdapterRegistry) -> None:
    assert {platform.value for platform in registry.platforms()} == {
        "youtube",
        "tiktok",
        "instagram",
        "facebook",
    }


def test_instagram_handle_is_cleaned(registry: AdapterRegistry) -> None:
    """A trailing slash must not leak into the handle."""
    parsed = registry.detect("https://www.instagram.com/nasa/")
    assert parsed.handle == "nasa"
