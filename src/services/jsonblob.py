"""Extraction of embedded JSON and meta tags from raw HTML.

This module is what replaces a headless browser: modern social pages ship their
entire state as JSON inside ``<script>`` tags or as ``og:`` meta tags, so a
balanced-brace scanner plus a tolerant tree walker is enough.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

from selectolax.lexbor import LexborHTMLParser as HTMLParser


def extract_balanced(text: str, start: int) -> str | None:
    """Return the balanced JSON object/array that starts at ``start``.

    ``start`` must point at ``{`` or ``[``. String literals and escapes are
    respected, so braces inside strings do not break the scan.
    """
    if start < 0 or start >= len(text):
        return None
    opener = text[start]
    if opener not in "{[":
        return None
    closer = "}" if opener == "{" else "]"

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def loads_or_none(raw: str | None) -> Any | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


def find_json_after(text: str, needle: str, *, start_at: int = 0) -> Any | None:
    """Find ``needle`` then parse the first balanced JSON value after it."""
    index = text.find(needle, start_at)
    if index == -1:
        return None
    return parse_json_at_first_bracket(text, index + len(needle))


def find_all_json_after(text: str, needle: str, *, limit: int = 20) -> Iterator[Any]:
    """Yield every JSON value that follows an occurrence of ``needle``."""
    start = 0
    found = 0
    while found < limit:
        index = text.find(needle, start)
        if index == -1:
            return
        value = parse_json_at_first_bracket(text, index + len(needle))
        start = index + len(needle)
        if value is not None:
            found += 1
            yield value


def parse_json_at_first_bracket(text: str, start_at: int) -> Any | None:
    """Skip whitespace/``=``/``:`` from ``start_at`` then parse the JSON value found."""
    index = start_at
    length = len(text)
    while index < length and text[index] in " \t\r\n=:":
        index += 1
        if index - start_at > 200:
            return None
    if index >= length or text[index] not in "{[":
        return None
    raw = extract_balanced(text, index)
    return loads_or_none(raw)


# --------------------------------------------------------------------- meta tags


def parse_html(html: str) -> HTMLParser:
    return HTMLParser(html)


def meta_content(html: str, *, property_name: str | None = None, name: str | None = None) -> str | None:
    """Return the ``content`` of a meta tag matched by property or name."""
    tree = parse_html(html)
    if property_name:
        node = tree.css_first(f'meta[property="{property_name}"]')
        if node is not None and node.attributes.get("content"):
            return node.attributes["content"]
    if name:
        node = tree.css_first(f'meta[name="{name}"]')
        if node is not None and node.attributes.get("content"):
            return node.attributes["content"]
    return None


def og_tags(html: str) -> dict[str, str]:
    """Collect every ``og:*`` meta tag into a dict keyed without the prefix."""
    tags: dict[str, str] = {}
    tree = parse_html(html)
    for node in tree.css('meta[property^="og:"]'):
        prop = node.attributes.get("property")
        content = node.attributes.get("content")
        if prop and content:
            tags[prop[3:]] = content
    for node in tree.css('meta[name^="twitter:"]'):
        prop = node.attributes.get("name")
        content = node.attributes.get("content")
        if prop and content:
            tags.setdefault(prop[8:], content)
    return tags


def script_content(html: str, *, element_id: str) -> str | None:
    """Return the text of ``<script id="...">``."""
    tree = parse_html(html)
    node = tree.css_first(f'script#{element_id}')
    if node is None:
        return None
    return node.text() or node.attributes.get("data-content")


def script_json(html: str, *, element_id: str) -> Any | None:
    return loads_or_none(script_content(html, element_id=element_id))


def document_title(html: str) -> str | None:
    tree = parse_html(html)
    node = tree.css_first("title")
    if node is None:
        return None
    return (node.text() or "").strip() or None


# --------------------------------------------------------------------- tree walking


def walk(value: Any) -> Iterator[Any]:
    """Depth-first iteration over every dict/list node in a JSON tree."""
    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def find_first(root: Any, predicate: Callable[[Any], bool]) -> Any | None:
    for node in walk(root):
        try:
            if predicate(node):
                return node
        except (KeyError, TypeError, IndexError):
            continue
    return None


def find_all(root: Any, predicate: Callable[[Any], bool]) -> list[Any]:
    results: list[Any] = []
    for node in walk(root):
        try:
            if predicate(node):
                results.append(node)
        except (KeyError, TypeError, IndexError):
            continue
    return results


def find_key(root: Any, key: str) -> Any | None:
    """Return the first value stored under ``key`` anywhere in the tree."""
    for node in walk(root):
        if isinstance(node, dict) and key in node:
            return node[key]
    return None


def find_all_keys(root: Any, key: str) -> list[Any]:
    return [node[key] for node in walk(root) if isinstance(node, dict) and key in node]


def dig(root: Any, *path: str | int) -> Any | None:
    """Safe nested lookup: ``dig(obj, "a", 0, "b")``."""
    current = root
    for key in path:
        if current is None:
            return None
        if isinstance(key, int):
            if not isinstance(current, list) or key >= len(current):
                return None
            current = current[key]
        else:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
    return current


def text_content(node: Any) -> str | None:
    """YouTube's ``{"simpleText": ...}`` / ``{"runs": [...]}`` text wrapper."""
    if node is None:
        return None
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if "simpleText" in node:
            return node["simpleText"]
        if "content" in node and isinstance(node["content"], str):
            return node["content"]
        runs = node.get("runs")
        if isinstance(runs, list):
            return "".join(str(run.get("text", "")) for run in runs if isinstance(run, dict))
    return None
