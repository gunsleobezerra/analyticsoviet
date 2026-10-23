"""Startup banner: cookie inventory, color/unicode degradation, opt-out."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from core.banner import (
    banner_enabled,
    color_enabled,
    count_cookies,
    inspect_cookies,
    print_startup_banner,
    render_startup_banner,
)
from core.config import Settings

NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    "# https://curl.se/docs/http-cookies.html\n"
    "\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\tabc\n"
    "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1893456000\tHSID\tdef\n"
    ".youtube.com\tTRUE\t/\tFALSE\t1893456000\tPREF\tghi\n"
)


@pytest.fixture
def cookie_dir(tmp_path: Path) -> Path:
    (tmp_path / "youtube.txt").write_text(NETSCAPE, encoding="utf-8")
    return tmp_path


def test_count_cookies_handles_comments_and_httponly(cookie_dir: Path) -> None:
    assert count_cookies(cookie_dir / "youtube.txt") == 3


def test_count_cookies_missing_file_returns_zero(tmp_path: Path) -> None:
    assert count_cookies(tmp_path / "nope.txt") == 0


def test_inspect_cookies_reports_present_and_missing(cookie_dir: Path) -> None:
    settings = Settings(environment="test", cookies_dir=cookie_dir)
    infos = {info.platform: info for info in inspect_cookies(settings)}

    assert set(infos) == {"youtube", "tiktok", "instagram", "facebook"}
    assert infos["youtube"].exists is True
    assert infos["youtube"].cookies == 3
    assert infos["youtube"].size > 0
    assert infos["tiktok"].exists is False


def test_inspect_cookies_marks_s3_source(cookie_dir: Path) -> None:
    settings = Settings(environment="test", cookies_dir=cookie_dir)
    infos = {info.platform: info for info in inspect_cookies(settings, ["youtube.txt"])}
    assert infos["youtube"].source == "s3"


def test_render_without_color_has_no_ansi(cookie_dir: Path) -> None:
    settings = Settings(environment="test", cookies_dir=cookie_dir)
    text = render_startup_banner(
        settings,
        inspect_cookies(settings),
        color=False,
        unicode=True,
        elapsed_ms=12.5,
    )

    assert "\x1b[" not in text
    assert "API online" in text
    assert "youtube" in text
    assert "missing" in text
    assert "boot 12 ms" in text


def test_render_ascii_fallback_uses_ascii_markers(cookie_dir: Path) -> None:
    settings = Settings(environment="test", cookies_dir=cookie_dir)
    text = render_startup_banner(settings, inspect_cookies(settings), color=False, unicode=False)

    assert "☭" not in text
    assert "─" not in text
    assert "+" in text


def test_render_reports_no_cookies(tmp_path: Path) -> None:
    settings = Settings(environment="test", cookies_dir=tmp_path)
    text = render_startup_banner(settings, inspect_cookies(settings), color=False)
    assert "no cookie files found" in text


def test_banner_enabled_defaults_to_not_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANALYTICSOVIET_BANNER", raising=False)
    assert banner_enabled(json_logs=False) is True
    assert banner_enabled(json_logs=True) is False

    monkeypatch.setenv("ANALYTICSOVIET_BANNER", "1")
    assert banner_enabled(json_logs=True) is True

    monkeypatch.setenv("ANALYTICSOVIET_BANNER", "off")
    assert banner_enabled(json_logs=False) is False


def test_color_disabled_for_json_and_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)

    assert color_enabled(stream) is False  # StringIO is not a TTY
    assert color_enabled(stream, json_logs=True) is False

    monkeypatch.setenv("FORCE_COLOR", "1")
    assert color_enabled(stream, json_logs=True) is True

    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert color_enabled(stream) is False


def test_print_suppressed_in_json_mode(
    cookie_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANALYTICSOVIET_BANNER", raising=False)
    stream = io.StringIO()
    settings = Settings(environment="prod", cookies_dir=cookie_dir)

    assert print_startup_banner(settings, json_logs=True, stream=stream) is False
    assert stream.getvalue() == ""


def test_print_writes_to_stream(cookie_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANALYTICSOVIET_BANNER", raising=False)
    stream = io.StringIO()
    settings = Settings(environment="dev", cookies_dir=cookie_dir)

    assert print_startup_banner(settings, elapsed_ms=5.0, json_logs=False, stream=stream) is True
    assert "ANALYTICSOVIET" in stream.getvalue().replace(" ", "")
