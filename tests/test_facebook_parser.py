"""Facebook parser tests: login walls must never leak into profile fields."""

from __future__ import annotations

import pytest
from tests.conftest import load_fixture

import platforms.facebook as fb


@pytest.fixture(scope="module")
def mbasic_html() -> str:
    return load_fixture("facebook_mbasic.html.gz")


def test_first_param_prefers_first_present_key() -> None:
    query = {"story_fbid": ["111"], "id": ["222"]}
    assert fb._first_param(query, "story_fbid", "fbid") == "111"
    assert fb._first_param(query, "fbid", "story_fbid") == "111"
    assert fb._first_param(query, "missing") is None


def test_first_param_skips_empty_values() -> None:
    query = {"story_fbid": [""], "fbid": ["999"]}
    assert fb._first_param(query, "story_fbid", "fbid") == "999"


def test_first_param_handles_empty_list() -> None:
    assert fb._first_param({"id": []}, "id") is None


@pytest.mark.parametrize(
    "title",
    [
        "Entre ou cadastre-se para visualizar",
        "Log in or sign up to view",
        "Facebook",
        "  facebook  ",
    ],
)
def test_placeholder_titles_are_rejected(title: str) -> None:
    html = f'<html><head><meta property="og:title" content="{title}" /></head></html>'
    assert fb._profile_from_html(html, "nasa") is None


@pytest.mark.parametrize(
    "description",
    [
        "Veja posts, fotos e muito mais no Facebook.",
        "See posts, photos and more on Facebook.",
    ],
)
def test_placeholder_descriptions_are_rejected(description: str) -> None:
    html = f'<html><head><meta property="og:description" content="{description}" /></head></html>'
    assert fb._profile_from_html(html, "nasa") is None


def test_real_page_is_parsed() -> None:
    html = (
        "<html><head>"
        '<meta property="og:title" content="NASA - National Aeronautics and Space Administration | Facebook" />'
        '<meta property="og:description" '
        'content="NASA. 28,744,029 likes · 1,234,567 talking about this. Explore the universe." />'
        '<meta property="og:image" content="https://cdn/nasa.jpg" />'
        "</head></html>"
    )
    profile = fb._profile_from_html(html, "nasa")
    assert profile is not None
    assert profile.display_name == "NASA - National Aeronautics and Space Administration"
    assert profile.metrics.followers == 28_744_029
    assert profile.extra["talking_about"] == 1_234_567
    assert profile.avatar_url == "https://cdn/nasa.jpg"


def test_captured_login_wall_fixture_yields_no_profile(mbasic_html: str) -> None:
    """The captured mbasic page is a wall; parsing it must fail, not invent data."""
    profile = fb._profile_from_html(mbasic_html, "nasa")
    assert profile is None


def test_metric_helper_returns_none_on_no_match() -> None:
    assert fb._metric("no numbers here", fb._LIKES_RE) is None


def test_published_from_html_returns_none_when_absent() -> None:
    assert fb._published_from_html("<html></html>") is None
