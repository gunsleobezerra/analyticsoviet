"""Tests for the per-field metadata and the configuration contract.

The completeness test is the important one: it fails when a field is added to
:class:`Settings` without metadata, so a new setting cannot reach production
without a section, a label and a class.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.config import Settings
from core.config_fields import (
    FIELDS,
    FIELDS_BY_ATTRIBUTE,
    FIELDS_BY_NAME,
    FieldClass,
    FieldKind,
    Section,
    fields_by_section,
    sensitive_names,
    settings_field_names,
    spec_for,
)
from core.config_store import CacheFamily, ConfigStore, EffectiveSettings, FieldValue, ValueOrigin

# --------------------------------------------------------------------------- #
# completeness
# --------------------------------------------------------------------------- #


def test_every_setting_has_metadata() -> None:
    declared = settings_field_names()
    described = set(FIELDS_BY_NAME)

    assert declared - described == set(), "settings without metadata"
    assert described - declared == set(), "metadata for settings that no longer exist"


def test_metadata_names_are_unique() -> None:
    names = [spec.name for spec in FIELDS]
    assert len(names) == len(set(names))
    assert len(FIELDS_BY_ATTRIBUTE) == len(FIELDS)


def test_every_field_is_renderable() -> None:
    for spec in FIELDS:
        assert spec.label.strip(), f"{spec.name} has no label"
        assert spec.help.strip(), f"{spec.name} has no help text"
        assert isinstance(spec.section, Section)
        assert isinstance(spec.kind, FieldKind)
        assert isinstance(spec.cls, FieldClass)


def test_secrets_are_typed_as_secrets() -> None:
    for spec in FIELDS:
        if spec.sensitive:
            assert spec.kind is FieldKind.SECRET, f"{spec.name} is sensitive but not a secret input"
            assert spec.cls is not FieldClass.READ_ONLY


# --------------------------------------------------------------------------- #
# classes
# --------------------------------------------------------------------------- #


def test_environment_only_fields() -> None:
    env_only = {spec.name for spec in FIELDS if spec.cls is FieldClass.ENV_ONLY}
    assert env_only == {"API_AUTH_ENABLED", "MCP_ENABLED", "APP_ENCRYPTION_KEY"}

    for name in env_only:
        spec = spec_for(name)
        assert not spec.writable, f"{name} must refuse a database override"
        assert spec.requires_restart


def test_proxy_and_timeouts_are_cold() -> None:
    for name in (
        "PROXY_URL",
        "HTTP_TIMEOUT_SECONDS",
        "HTTP_CONNECT_TIMEOUT_SECONDS",
        "HTTP_MAX_CONNECTIONS",
        "HTTP_MAX_KEEPALIVE_CONNECTIONS",
    ):
        spec = spec_for(name)
        assert spec.cls is FieldClass.COLD, f"{name} must require a restart"
        assert spec.requires_restart


def test_read_only_fields() -> None:
    read_only = {spec.name for spec in FIELDS if spec.cls is FieldClass.READ_ONLY}
    assert read_only == {"APP_NAME", "APP_VERSION"}
    assert all(not spec_for(name).writable for name in read_only)


def test_cache_ttls_are_hot_and_carry_a_family() -> None:
    ttl_fields = {
        "CACHE_TTL_PROFILE_SECONDS": CacheFamily.PROFILE,
        "CACHE_TTL_POSTS_SECONDS": CacheFamily.POSTS,
        "CACHE_TTL_POST_SECONDS": CacheFamily.POST,
        "CACHE_TTL_BLOCKED_SECONDS": CacheFamily.BLOCKED,
    }
    for name in ttl_fields:
        assert spec_for(name).cls is FieldClass.HOT
    assert {family.value for family in CacheFamily} == {"profile", "posts", "post", "blocked"}


def test_lookup_accepts_both_spellings() -> None:
    assert spec_for("LOG_LEVEL") is spec_for("log_level")
    with pytest.raises(KeyError):
        spec_for("NOT_A_FIELD")


def test_sections_cover_every_field() -> None:
    grouped = fields_by_section()
    assert sum(len(specs) for specs in grouped.values()) == len(FIELDS)


def test_sensitive_names_are_the_secret_fields() -> None:
    assert sensitive_names() == {spec.name for spec in FIELDS if spec.sensitive}
    assert "APP_ENCRYPTION_KEY" in sensitive_names()
    assert "YOUTUBE_API_KEY" in sensitive_names()
    assert "LOG_LEVEL" not in sensitive_names()


# --------------------------------------------------------------------------- #
# effective settings
# --------------------------------------------------------------------------- #


def _effective(origins: dict[str, ValueOrigin]) -> EffectiveSettings:
    return EffectiveSettings(
        settings=Settings(environment="test", log_level="DEBUG"),
        origins=origins,
        version=7,
        loaded_at=datetime.now(UTC),
    )


def test_effective_settings_delegates_to_settings() -> None:
    effective = _effective({"LOG_LEVEL": ValueOrigin.DATABASE})
    assert effective.log_level == "DEBUG"
    assert effective.value_of("LOG_LEVEL") == "DEBUG"
    assert effective.version == 7


def test_effective_settings_reports_origin_and_pending_restart() -> None:
    effective = _effective(
        {
            "LOG_LEVEL": ValueOrigin.DATABASE,
            "DATABASE_URL": ValueOrigin.DATABASE,
            "YOUTUBE_API_KEY": ValueOrigin.VAULT,
        }
    )

    assert effective.origin_of("LOG_LEVEL") is ValueOrigin.DATABASE
    assert effective.origin_of("RATELIMIT_YOUTUBE_PER_MINUTE") is ValueOrigin.DEFAULT
    assert effective.is_set("YOUTUBE_API_KEY")
    assert not effective.is_set("MCP_ENABLED")
    # LOG_LEVEL is hot, so a database value applies at once; DATABASE_URL is cold.
    assert effective.pending_restart() == ("DATABASE_URL",)


def test_effective_settings_withholds_secret_values() -> None:
    effective = _effective({"YOUTUBE_API_KEY": ValueOrigin.VAULT})

    public = effective.as_public_dict()
    assert public["YOUTUBE_API_KEY"] is None
    assert public["LOG_LEVEL"] == "DEBUG"

    views = {view.name: view for view in effective.views()}
    assert len(views) == len(FIELDS)
    secret_view = views["YOUTUBE_API_KEY"]
    assert isinstance(secret_view, FieldValue)
    assert secret_view.value is None
    assert secret_view.is_set
    assert secret_view.editable  # hot field: the panel may set it
    assert not views["APP_ENCRYPTION_KEY"].editable  # environment-only
    assert views["LOG_LEVEL"].value == "DEBUG"


def test_effective_settings_marks_pending_restart_on_the_field() -> None:
    effective = _effective({"PROXY_URL": ValueOrigin.DATABASE})
    views = {view.name: view for view in effective.views()}
    assert views["PROXY_URL"].pending_restart
    assert not views["LOG_LEVEL"].pending_restart


def test_config_store_is_a_contract() -> None:
    with pytest.raises(TypeError):
        ConfigStore()  # type: ignore[abstract]
