"""Offline contract tests for the curated profile list.

``tests/test_profiles.txt`` holds real profile URLs (one per line). These tests
never touch the network: they only assert that every URL in the list is
recognised, normalised and round-trippable. The live counterpart lives in
``tests/live/test_profiles_live.py`` and is skipped unless ``LIVE=1``.
"""

from __future__ import annotations

import pytest

from api.urls import resolve_profile_url
from models.schemas import Platform
from platforms.registry import AdapterRegistry
from profile_cases import PROFILES_FILE, load_cases

CASES = load_cases()
IDS = [case.id for case in CASES]


def test_profile_list_is_not_empty() -> None:
    assert PROFILES_FILE.exists()
    assert CASES, f"no profile URLs found in {PROFILES_FILE}"


def test_profile_list_has_no_duplicates() -> None:
    urls = [case.url for case in CASES]
    duplicates = {url for url in urls if urls.count(url) > 1}
    assert not duplicates, f"duplicate URLs in {PROFILES_FILE}: {sorted(duplicates)}"


def test_every_case_platform_is_enabled(registry: AdapterRegistry) -> None:
    enabled = {platform.value for platform in registry.platforms()}
    missing = sorted({case.platform for case in CASES} - enabled)
    assert not missing, f"platforms in the list but not enabled: {missing}"


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_profile_url_is_detected(registry: AdapterRegistry, case) -> None:
    parsed = registry.detect(case.url)

    assert parsed.platform == Platform(case.platform)
    assert parsed.kind == "profile"
    assert parsed.handle == case.handle
    assert parsed.resource_id is None
    assert parsed.canonical_url.startswith("https://")


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_detection_is_idempotent(registry: AdapterRegistry, case) -> None:
    first = registry.detect(case.url)
    second = registry.detect(first.canonical_url)

    assert second.platform == first.platform
    assert second.kind == "profile"
    assert second.handle == first.handle
    assert second.canonical_url.rstrip("/") == first.canonical_url.rstrip("/")


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_profile_resolves_from_url_and_from_bare_handle(registry: AdapterRegistry, case) -> None:
    adapter = registry.get(Platform(case.platform))

    from_url = resolve_profile_url(adapter, Platform(case.platform), case.url)
    from_handle = resolve_profile_url(adapter, Platform(case.platform), case.handle)

    assert from_url is not None
    assert from_handle is not None
    assert from_url.handle == case.handle
    assert from_handle.handle == case.handle
    assert from_handle.canonical_url.rstrip("/") == from_url.canonical_url.rstrip("/")


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_profile_capabilities_advertise_profiles(registry: AdapterRegistry, case) -> None:
    adapter = registry.get(Platform(case.platform))
    assert adapter.capabilities.supports_profile is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/__a/",
        "https://www.instagram.com/_u/",
        "https://www.instagram.com/p/CxYzAbCdEfG/",
    ],
)
def test_instagram_internal_paths_are_not_profiles(registry: AdapterRegistry, url: str) -> None:
    parsed = registry.detect_or_none(url)
    assert parsed is None or parsed.kind != "profile"
