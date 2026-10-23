"""Unit tests for the human-number/date helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.human import clean_text, parse_count, parse_duration, parse_relative_date


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("8,8 mi", 8_800_000),
        ("1.2M views", 1_200_000),
        ("214 mi", 214_000_000),
        ("1.234 visualizações", 1_234),
        ("1.2K", 1_200),
        ("15,4 mil", 15_400),
        ("12 345", 12_345),
        ("1.2B", 1_200_000_000),
        ("0", 0),
        ("", None),
        (None, None),
        ("sem número", None),
    ],
)
def test_parse_count(raw: str | None, expected: int | None) -> None:
    assert parse_count(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("29:05", 1745.0),
        ("1:02:03", 3723.0),
        ("12", 12.0),
        ("", None),
        (None, None),
    ],
)
def test_parse_duration(raw: str | None, expected: float | None) -> None:
    assert parse_duration(raw) == expected


def test_parse_relative_date_portuguese() -> None:
    now = datetime(2026, 1, 31, 12, 0, tzinfo=UTC)
    assert parse_relative_date("há 1 dia", now=now) == now - timedelta(days=1)
    assert parse_relative_date("há 2 semanas", now=now) == now - timedelta(weeks=2)


def test_parse_relative_date_english() -> None:
    now = datetime(2026, 1, 31, 12, 0, tzinfo=UTC)
    assert parse_relative_date("3 days ago", now=now) == now - timedelta(days=3)
    assert parse_relative_date("Streamed 1 year ago", now=now) is not None


def test_parse_relative_date_never_invents_a_date() -> None:
    """Absolute or unparseable text must yield ``None``, never "now"."""
    now = datetime(2026, 1, 31, 12, 0, tzinfo=UTC)
    assert parse_relative_date("25 de junho de 2026", now=now) is None
    assert parse_relative_date("", now=now) is None
    assert parse_relative_date(None, now=now) is None


def test_clean_text_collapses_whitespace() -> None:
    assert clean_text("  hello\n\n  world\t ") == "hello world"
    assert clean_text("") is None
    assert clean_text(None) is None
