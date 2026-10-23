"""Unit tests for the HTML/JSON extraction helpers."""

from __future__ import annotations

import json

from services.jsonblob import (
    dig,
    extract_balanced,
    find_all,
    find_json_after,
    find_key,
    loads_or_none,
    og_tags,
    parse_html,
    script_json,
    text_content,
    walk,
)


def test_extract_balanced_handles_nested_objects() -> None:
    text = 'prefix {"a": {"b": [1, 2, {"c": "}"}]}, "d": 1} suffix'
    start = text.index("{")
    assert extract_balanced(text, start) == '{"a": {"b": [1, 2, {"c": "}"}]}, "d": 1}'


def test_extract_balanced_ignores_braces_inside_strings() -> None:
    text = '{"a": "}{"}'
    assert extract_balanced(text, 0) == '{"a": "}{"}'


def test_extract_balanced_returns_none_when_unterminated() -> None:
    assert extract_balanced('{"a": 1', 0) is None


def test_find_json_after_locates_assignment() -> None:
    html = "<script>var ytInitialData = {\"a\": [1, 2, 3]};</script>"
    assert find_json_after(html, "ytInitialData") == {"a": [1, 2, 3]}


def test_find_json_after_returns_none_when_missing() -> None:
    assert find_json_after("<html></html>", "ytInitialData") is None


def test_loads_or_none() -> None:
    assert loads_or_none('{"a": 1}') == {"a": 1}
    assert loads_or_none("not json") is None
    assert loads_or_none(None) is None


def test_og_tags_and_meta_content() -> None:
    html = """
    <html><head>
      <meta property="og:title" content="NASA on Instagram" />
      <meta name="description" content="explore the universe" />
      <title>Instagram</title>
    </head></html>
    """
    tags = og_tags(html)
    assert tags["title"] == "NASA on Instagram"
    assert og_tags("<html></html>") == {}


def test_script_json_reads_element_by_id() -> None:
    payload = {"scope": {"webapp.user-detail": {"ok": True}}}
    html = f'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">{json.dumps(payload)}</script>'
    assert script_json(html, element_id="__UNIVERSAL_DATA_FOR_REHYDRATION__") == payload
    assert script_json(html, element_id="nope") is None


def test_dig_walks_dicts_and_lists() -> None:
    root = {"a": {"b": [{"c": 42}]}}
    assert dig(root, "a", "b", 0, "c") == 42
    assert dig(root, "a", "missing") is None
    assert dig(None, "a") is None


def test_find_key_is_depth_first() -> None:
    root = {"x": {"y": {"target": 7}}}
    assert find_key(root, "target") == 7
    assert find_key(root, "absent") is None


def test_find_all_collects_every_match() -> None:
    root = {"items": [{"videoRenderer": 1}, {"videoRenderer": 2}, {"other": 3}]}
    matches = find_all(root, lambda node: isinstance(node, dict) and "videoRenderer" in node)
    assert sorted(match["videoRenderer"] for match in matches) == [1, 2]


def test_walk_yields_nested_values() -> None:
    root = {"a": [1, {"b": 2}]}
    values = list(walk(root))
    # Traversal order between dict values and list items is not contractual.
    assert root in values
    assert root["a"] in values
    assert 1 in values and 2 in values and {"b": 2} in values


def test_parse_html_and_text_content() -> None:
    tree = parse_html("<div><span>hello</span> <b>world</b></div>")
    assert tree.css_first("span") is not None
    assert text_content({"simpleText": "hi"}) == "hi"
    assert text_content({"runs": [{"text": "a"}, {"text": "b"}]}) == "ab"
