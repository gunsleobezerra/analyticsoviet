"""YouTube parser tests driven by a captured channel snapshot."""

from __future__ import annotations

import pytest
from tests.conftest import load_fixture

import platforms.youtube as yt
from platforms.youtube import (
    _channel_handle,
    _channel_title,
    _find_metric,
    _post_from_lockup,
    _post_from_shorts_lockup,
    _post_from_video_renderer,
    _valid_video_id,
)
from services.jsonblob import find_all, find_json_after


@pytest.fixture(scope="module")
def videos_root() -> object:
    html = load_fixture("youtube_videos.html.gz")
    root = find_json_after(html, "ytInitialData")
    assert root is not None, "fixture no longer contains ytInitialData"
    return root


@pytest.fixture(scope="module")
def channel_root() -> object:
    html = load_fixture("youtube_channel.html.gz")
    root = find_json_after(html, "ytInitialData")
    assert root is not None, "fixture no longer contains ytInitialData"
    return root


def test_lockup_view_model_is_parsed(videos_root: object) -> None:
    """2025+ channel grids ship ``lockupViewModel`` instead of ``videoRenderer``.

    Regression guard: this markup change silently produced ``posts: 0``.
    """
    nodes = find_all(videos_root, lambda node: isinstance(node, dict) and "lockupViewModel" in node)
    assert nodes, "fixture has no lockupViewModel entries"

    posts = [post for post in (_post_from_lockup(node["lockupViewModel"]) for node in nodes) if post is not None]
    assert len(posts) >= 3

    first = posts[0]
    assert first.id and len(first.id) == 11
    assert first.url == f"https://www.youtube.com/watch?v={first.id}"
    assert first.caption
    assert first.duration_seconds and first.duration_seconds > 0
    assert first.metrics.views and first.metrics.views > 0
    assert first.extra["via"] == "lockup"


def test_lockup_reports_source_text_for_relative_dates(videos_root: object) -> None:
    nodes = find_all(videos_root, lambda node: isinstance(node, dict) and "lockupViewModel" in node)
    post = _post_from_lockup(nodes[0]["lockupViewModel"])
    assert post is not None
    assert "published_text" in post.extra
    assert "views_text" in post.extra


def test_lockup_rejects_non_dict() -> None:
    assert _post_from_lockup(None) is None
    assert _post_from_lockup("nope") is None
    assert _post_from_lockup({}) is None


def test_lockup_requires_valid_content_id() -> None:
    assert _post_from_lockup({"contentId": "short"}) is None
    assert _post_from_lockup({"contentId": 123}) is None


def test_shorts_lockup_is_parsed(channel_root: object) -> None:
    nodes = find_all(channel_root, lambda node: isinstance(node, dict) and "shortsLockupViewModel" in node)
    assert nodes, "fixture has no shortsLockupViewModel entries"
    posts = [post for post in (_post_from_shorts_lockup(node["shortsLockupViewModel"]) for node in nodes) if post]
    assert posts
    assert all(post.media_type == "short" for post in posts)


def test_video_renderer_still_parsed() -> None:
    renderer = {
        "videoId": "9GSDvO0LFFE",
        "title": {"runs": [{"text": "A video title"}]},
        "viewCountText": {"simpleText": "1,2 mi de visualizações"},
        "lengthText": {"simpleText": "29:05"},
        "publishedTimeText": {"simpleText": "há 1 dia"},
    }
    post = _post_from_video_renderer(renderer)
    assert post is not None
    assert post.id == "9GSDvO0LFFE"
    assert post.metrics.views == 1_200_000
    assert post.duration_seconds == 1745.0
    assert post.published_at is not None
    assert post.extra["via"] == "channel_videos_tab"


def test_video_renderer_rejects_invalid_id() -> None:
    assert _post_from_video_renderer({"videoId": "too-short"}) is None


def test_channel_metadata(channel_root: object) -> None:
    assert _channel_title(channel_root) == "Marques Brownlee"
    assert _channel_handle(channel_root) == "mkbhd"
    assert _find_metric(channel_root, yt._VIDEO_COUNT_RE) == 1000


@pytest.mark.parametrize(
    ("video_id", "expected"),
    [
        ("9GSDvO0LFFE", True),
        ("abc12345678", True),
        ("_gCXmKjDecU", True),
        ("mk_wdHePbtQ", True),
        ("short", False),
        ("waytoolongvideoid", False),
        ("bad chars!!!", False),
        (None, False),
        ("", False),
    ],
)
def test_valid_video_id(video_id: str | None, expected: bool) -> None:
    assert _valid_video_id(video_id) is expected
