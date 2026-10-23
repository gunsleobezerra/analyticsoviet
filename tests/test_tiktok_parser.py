"""TikTok parser tests, focused on the 32-bit counter wraparound bug."""

from __future__ import annotations

import pytest
from tests.conftest import load_fixture

import platforms.tiktok as tt
from services.jsonblob import script_json


@pytest.fixture(scope="module")
def payload() -> dict[str, object]:
    html = load_fixture("tiktok_profile.html.gz")
    data = script_json(html, element_id=tt._REHYDRATION_ID)
    assert isinstance(data, dict), "fixture no longer contains the rehydration payload"
    return data


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2690368194", 2_690_368_194),
        (2_690_368_194, 2_690_368_194),
        # TikTok serialises heartCount as a signed 32-bit int that has wrapped.
        (-1_604_599_102, 2_690_368_194),
        (-1_604_599_102 % 2**32, 2_690_368_194),
        ("1.2M", 1_200_000),
        ("163M", 163_000_000),
        (0, 0),
        (None, None),
        (True, None),
        ("", None),
        ("not a number", None),
    ],
)
def test_coerce_count(raw: object, expected: int | None) -> None:
    assert tt._coerce_count(raw) == expected


def test_int_reads_wrapped_heart_count() -> None:
    assert tt._int({"heartCount": -1_604_599_102}, "heartCount") == 2_690_368_194
    assert tt._int({"heartCount": "2690368194"}, "heartCount") == 2_690_368_194
    assert tt._int({}, "heartCount") is None


def test_find_stats_merges_every_block(payload: dict[str, object]) -> None:
    stats = tt._find_stats(payload)
    assert stats is not None
    assert stats["followerCount"] == 163_000_000
    assert stats["followingCount"] == 82
    assert stats["videoCount"] == 1359


def test_find_stats_returns_none_for_empty_input() -> None:
    assert tt._find_stats({}) is None
    assert tt._find_stats(None) is None


def test_find_stats_requires_follower_count() -> None:
    assert tt._find_stats({"stats": {"diggCount": 1}}) is None


def test_rehydration_payload_round_trips(payload: dict[str, object]) -> None:
    scope = payload.get("__DEFAULT_SCOPE__")
    assert isinstance(scope, dict)
    assert "webapp.user-detail" in scope


def test_anonymous_payload_has_no_post_list(payload: dict[str, object]) -> None:
    """Anonymous TikTok HTML embeds counters but not the video list.

    This is a platform limitation, not a parser bug: the adapter must degrade
    to ``partial`` with an explicit warning instead of inventing posts.
    """
    from services.jsonblob import find_all

    items = find_all(payload, lambda node: isinstance(node, dict) and "itemStruct" in node)
    assert items == []
