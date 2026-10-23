"""Instagram parser tests: the anonymous surface is a login wall."""

from __future__ import annotations

import pytest
from tests.conftest import load_fixture

import platforms.instagram as ig
from services.jsonblob import og_tags


@pytest.fixture(scope="module")
def wall_html() -> str:
    return load_fixture("instagram_login_wall.html.gz")


def test_fixture_really_is_a_login_wall(wall_html: str) -> None:
    tags = og_tags(wall_html)
    # The logged-out shell has no profile description at all, which is exactly
    # the signal the adapter uses.
    assert "description" not in tags
    assert ig._looks_like_login_wall(wall_html) is True


def test_login_wall_never_yields_a_profile(wall_html: str) -> None:
    """No counters, no profile: the adapter must not fabricate one."""
    assert ig._profile_from_html(wall_html, "nasa") is None


def test_embedded_profile_absent_on_login_wall(wall_html: str) -> None:
    assert ig._embedded_profile(wall_html) is None


def test_posts_absent_on_login_wall(wall_html: str) -> None:
    assert ig._posts_from_html(wall_html, limit=5) == []


@pytest.mark.parametrize("title", sorted(ig._PLACEHOLDER_TITLES))
def test_placeholder_titles_are_rejected(title: str) -> None:
    html = f'<html><head><title>{title}</title></head><body></body></html>'
    assert ig._profile_from_html(html, "nasa") is None


def test_profile_description_is_parsed() -> None:
    html = (
        '<html><head><meta property="og:title" content="NASA (@nasa) &#8226; Instagram photos and videos" />'
        '<meta property="og:description" content="12.3M Followers, 78 Following, 4,321 Posts" />'
        "</head></html>"
    )
    profile = ig._profile_from_html(html, "nasa")
    assert profile is not None
    assert profile.handle == "nasa"
    assert profile.metrics.followers == 12_300_000
    assert profile.metrics.following == 78
    assert profile.metrics.posts == 4321


def test_post_from_og_parses_engagement() -> None:
    html = (
        '<html><head><meta property="og:image" content="https://cdn/x.jpg" />'
        '<meta property="og:description" '
        'content="1,234 likes, 56 comments - nasa on October 1, 2026: &quot;Webb sees it all.&quot;" />'
        "</head></html>"
    )
    post = ig._post_from_og(html, "CxYzAbCdEfG")
    assert post is not None
    assert post.id == "CxYzAbCdEfG"
    assert post.url == "https://www.instagram.com/p/CxYzAbCdEfG/"
    assert post.metrics.likes == 1234
    assert post.metrics.comments == 56
    assert post.thumbnail_url == "https://cdn/x.jpg"
    assert post.extra["via"] == "og_tags"


def test_post_from_og_returns_none_without_description() -> None:
    assert ig._post_from_og("<html></html>", "CxYzAbCdEfG") is None


def test_post_from_media_node_rejects_garbage() -> None:
    assert ig._post_from_media_node(None) is None
    assert ig._post_from_media_node("nope") is None


def test_captured_wall_is_detected_as_challenge(wall_html: str) -> None:
    """The stored fixture is a checkpoint page, not just a logged-out shell."""
    assert ig._looks_like_challenge(wall_html) is True


def test_plain_login_page_is_not_a_challenge() -> None:
    html = '<html><head><title>Login • Instagram</title></head><body></body></html>'
    assert ig._looks_like_challenge(html) is False


@pytest.mark.parametrize(
    "marker", ["checkpoint", "challenge_required", "/challenge/"]
)
def test_challenge_markers_are_recognised(marker: str) -> None:
    assert ig._looks_like_challenge(f"<html><script>{marker}</script></html>") is True
