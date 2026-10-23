"""Loader for the curated profile list in ``tests/test_profiles.txt``.

The file is plain text: one URL per line, ``#`` starts a comment, blank lines
are ignored. The expected platform/handle for each URL is derived from the URL
itself, so adding a link to the file is enough to add it to both the offline
contract tests and the live suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

PROFILES_FILE = Path(__file__).with_name("test_profiles.txt")

_HOSTS: dict[str, str] = {
    "instagram.com": "instagram",
    "instagr.am": "instagram",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "music.youtube.com": "youtube",
    "tiktok.com": "tiktok",
    "facebook.com": "facebook",
    "fb.com": "facebook",
    "fb.watch": "facebook",
    "mbasic.facebook.com": "facebook",
}


@dataclass(frozen=True, slots=True)
class ProfileCase:
    """One profile URL plus the platform/handle it must resolve to."""

    url: str
    platform: str
    handle: str

    @property
    def id(self) -> str:
        return f"{self.platform}:{self.handle}"


def expected_case(url: str) -> ProfileCase:
    """Derive ``(platform, handle)`` from a profile URL.

    Raises ``ValueError`` for URLs that cannot be a supported profile link, so a
    typo in ``test_profiles.txt`` fails loudly at collection time.
    """
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.netloc or "").lower().split(":")[0]
    for prefix in ("www.", "m.", "web."):
        host = host.removeprefix(prefix)

    platform = _HOSTS.get(host)
    if platform is None:
        raise ValueError(f"unsupported host in {url!r}: {host or '<empty>'}")

    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        raise ValueError(f"no profile handle in {url!r}")

    raw = segments[0]
    if platform in {"youtube", "tiktok"}:
        raw = raw[1:] if raw.startswith("@") else raw

    if not raw:
        raise ValueError(f"empty profile handle in {url!r}")
    return ProfileCase(url=url, platform=platform, handle=raw)


def load_cases(path: Path | str = PROFILES_FILE) -> list[ProfileCase]:
    """Parse the profile list, skipping blanks and ``#`` comments."""
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"missing profile list: {source}")

    cases: list[ProfileCase] = []
    for line_number, raw_line in enumerate(
        source.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            cases.append(expected_case(line))
        except ValueError as exc:  # pragma: no cover - defensive, points at the line
            raise ValueError(f"{source}:{line_number}: {exc}") from exc
    return cases


__all__ = ["PROFILES_FILE", "ProfileCase", "expected_case", "load_cases"]
