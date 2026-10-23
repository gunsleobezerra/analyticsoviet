"""Colored startup banner (hammer & sickle) with a cookie inventory.

The banner is written straight to a stream instead of going through structlog,
so it can never corrupt the JSON log stream. It is suppressed whenever logs are
rendered as JSON (production) unless ``ANALYTICSOVIET_BANNER`` says otherwise,
and colors are dropped when the stream is not a TTY, when ``NO_COLOR`` is set,
or when ``FORCE_COLOR=0``.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from models.schemas import Platform

if TYPE_CHECKING:
    from core.config import Settings

# --------------------------------------------------------------------- art
ART: tuple[str, ...] = (
    "                  #",
    "                ##                       ####       #### ############        #######     ####",
    "             ####                        ####       #### ############      ###########   ####",
    "           ####    ########              ####       ####     ####          ####    ####  ####",
    "          ####    ###########            ####       ####     ####  ######  ###     ####  ####",
    "        ####       ::=#########          ####       ####     ####  ######  ###     ####  ####",
    "       #####     :::::+##:::=#####       ####       ####     ####          ####    ###   ####",
    "      #####     :::::###::::#####        ########## ####     ####           ##########   ####",
    "      #####   :::::#####::::###          #########  ####     ####             #########  ####",
    "     ###### :::::::####::::                                                              ",
    "     ######  ::::::::::::::              ## ## ######  # ## ## ####### ############## #####",
    "     #######  ####*::::::::::            ############  ####################################",
    "      #############  :::::::                   ##                                        ",
    "       ##########    ::::    ####        #### ## ##  ### # ## ##  ####### ## ### # ## ## ##",
    "        ########### :::: ##########      ##########   ################### ## ###############",
    "        #############################                                                     ",
    "      #####################    ########  ####################################################",
    "     ######    #########         ######  ##++#+**+*-+*+#-###**=+++*=#==#-**=#++#+*-#:=#-**###",
    "      ####                         ##    ####################################################",
)

# ------------------------------------------------------------------ colors
_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_RED = "\x1b[91m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_CYAN = "\x1b[36m"
_MAGENTA = "\x1b[35m"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


@dataclass(slots=True, frozen=True)
class CookieInfo:
    """One platform's cookie file as seen at startup."""

    platform: str
    path: Path
    exists: bool
    cookies: int = 0
    size: int = 0
    source: str = "local"


def _flag(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return None


def banner_enabled(json_logs: bool) -> bool:
    """Whether the banner should be printed at all."""
    override = _flag("ANALYTICSOVIET_BANNER")
    if override is not None:
        return override
    return not json_logs


def color_enabled(stream: Any, json_logs: bool = False) -> bool:
    """ANSI colors only for an interactive stream, honoring NO_COLOR/FORCE_COLOR."""
    override = _flag("FORCE_COLOR")
    if override is not None:
        return override
    if os.getenv("NO_COLOR"):
        return False
    if json_logs:
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _paint(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{_RESET}" if enabled else text


def _unicode_ok(stream: Any) -> bool:
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        "☭─✔✖".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


# ----------------------------------------------------------------- cookies
def expected_cookie_path(settings: Settings, platform: str) -> Path:
    explicit = getattr(settings, f"cookies_{platform}_file", None)
    if explicit is not None:
        return Path(explicit)
    return Path(settings.cookies_dir) / f"{platform}.txt"


def count_cookies(path: Path) -> int:
    """Count entries in a Netscape cookie file (``#HttpOnly_`` lines included)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    total = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif line.startswith("#"):
            continue
        if len(line.split("\t")) >= 7:
            total += 1
    return total


def inspect_cookies(settings: Settings, downloaded: list[str] | None = None) -> list[CookieInfo]:
    """Report every platform's cookie file, whether or not it exists."""
    from_s3 = {name for name in (downloaded or [])}
    infos: list[CookieInfo] = []
    for platform in Platform:
        path = expected_cookie_path(settings, platform.value)
        exists = path.is_file()
        source = "s3" if path.name in from_s3 else "local"
        if exists:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            infos.append(
                CookieInfo(
                    platform=platform.value,
                    path=path,
                    exists=True,
                    cookies=count_cookies(path),
                    size=size,
                    source=source,
                )
            )
        else:
            infos.append(CookieInfo(platform=platform.value, path=path, exists=False, source=source))
    return infos


def _human_size(num: int) -> str:
    size = float(num)
    for unit in ("B", "KiB", "MiB"):
        if size < 1024 or unit == "MiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} MiB"


# ------------------------------------------------------------------ render
def render_startup_banner(
    settings: Settings,
    cookies: list[CookieInfo],
    *,
    color: bool = True,
    unicode: bool = True,
    elapsed_ms: float | None = None,
    downloaded: list[str] | None = None,
    s3_error: str | None = None,
) -> str:
    """Build the banner as a single string (trailing newline included)."""
    art_width = max(len(line) for line in ART)
    width = max(art_width + 2, 72)
    rule_char = "─" if unicode else "-"
    rule = _paint(rule_char * width, _DIM, color)
    mark = "☭" if unicode else "*"
    ok = "✔" if unicode else "+"
    bad = "✖" if unicode else "x"

    lines: list[str] = ["", rule]
    title = f"{mark}  A N A L Y T I C S O V I E T  {mark}"
    lines.append(_paint(title.center(width), _BOLD + _RED, color))
    subtitle = f"v{settings.app_version}  ·  env={settings.environment}  ·  api={settings.api_prefix}"
    lines.append(_paint(subtitle.center(width), _DIM, color))
    lines.append(rule)

    for art_line in ART:
        lines.append(_paint(art_line.center(width), _RED, color))

    lines.append(rule)

    status = f"{_paint(ok, _GREEN, color)} API {_paint('online', _GREEN + _BOLD, color)}"
    if elapsed_ms is not None:
        status += _paint(f"  (boot {elapsed_ms:.0f} ms)", _DIM, color)
    lines.append(f"  {status}")
    lines.append(
        "  "
        + _paint("docs      ", _CYAN, color)
        + f"/docs  ·  /redoc  ·  /openapi.json  ·  api {settings.api_prefix}"
    )
    lines.append(
        "  "
        + _paint("cache     ", _CYAN, color)
        + f"{settings.cache_backend}  ·  ttl profile={settings.cache_ttl_profile_seconds}s "
        + f"posts={settings.cache_ttl_posts_seconds}s  ·  history={_yn(settings.enable_history)}"
    )
    lines.append(
        "  "
        + _paint("http      ", _CYAN, color)
        + f"pool={settings.http_max_connections}  keepalive={settings.http_max_keepalive_connections}"
        + f"  timeout={settings.http_timeout_seconds:g}s  retries={settings.http_retries}"
    )
    lines.append(
        "  "
        + _paint("cookies   ", _CYAN, color)
        + f"backend={settings.cookies_storage_backend}"
        + _s3_note(downloaded, s3_error, color)
    )

    lines.append(rule)
    lines.append(_paint("  COOKIES", _BOLD + _MAGENTA, color))

    found = [info for info in cookies if info.exists]
    if found:
        for info in cookies:
            name = _paint(f"{info.platform:<10}", _BOLD, color)
            if info.exists:
                detail = (
                    f"{_paint(str(info.path), _DIM, color)}  "
                    f"{info.cookies:>3} cookies  {_human_size(info.size):>8}  "
                    f"{_paint(info.source, _CYAN, color)}"
                )
                lines.append(f"  {_paint(ok, _GREEN, color)} {name} {detail}")
            else:
                lines.append(
                    f"  {_paint(bad, _YELLOW, color)} {name} "
                    f"{_paint(str(info.path), _DIM, color)}  {_paint('missing', _YELLOW, color)}"
                )
        total = sum(info.cookies for info in found)
        lines.append(
            "  "
            + _paint(
                f"{len(found)}/{len(cookies)} platforms authenticated  ·  {total} cookies total",
                _DIM,
                color,
            )
        )
    else:
        lines.append(
            "  "
            + _paint(f"{bad} no cookie files found", _YELLOW, color)
            + _paint(f"  (looked in {settings.cookies_dir}/)", _DIM, color)
        )
        lines.append(
            "  " + _paint("scraping will run anonymously and may be rate-limited", _DIM, color)
        )

    lines.append(rule)
    lines.append("")
    return "\n".join(lines)


def _yn(value: bool) -> str:
    return "enabled" if value else "disabled"


def _s3_note(downloaded: list[str] | None, s3_error: str | None, color: bool) -> str:
    if s3_error:
        return "  ·  " + _paint(f"s3 sync failed: {s3_error}", _YELLOW, color)
    if downloaded is None:
        return ""
    return "  ·  " + _paint(f"s3 synced {len(downloaded)} file(s)", _GREEN, color)


def print_startup_banner(
    settings: Settings,
    *,
    downloaded: list[str] | None = None,
    s3_error: str | None = None,
    elapsed_ms: float | None = None,
    json_logs: bool = False,
    stream: Any | None = None,
) -> bool:
    """Print the banner to ``stream`` (stdout by default). Returns True if printed."""
    if not banner_enabled(json_logs):
        return False
    out = stream if stream is not None else sys.stdout
    text = render_startup_banner(
        settings,
        inspect_cookies(settings, downloaded),
        color=color_enabled(out, json_logs),
        unicode=_unicode_ok(out),
        elapsed_ms=elapsed_ms,
        downloaded=downloaded,
        s3_error=s3_error,
    )
    out.write(text)
    out.flush()
    return True


__all__ = [
    "ART",
    "CookieInfo",
    "banner_enabled",
    "color_enabled",
    "count_cookies",
    "expected_cookie_path",
    "inspect_cookies",
    "print_startup_banner",
    "render_startup_banner",
]
