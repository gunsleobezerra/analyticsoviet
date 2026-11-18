"""Contract tests for the persisted configuration field registry."""

from __future__ import annotations

import pytest

from core.config import Settings
from core.config_contract import (
    FIELDS,
    SECRET_KEYS,
    ConfigSource,
    Mutability,
    OverrideRejected,
    coerce_override,
    dump_override,
    get_field,
    iter_fields,
    load_override,
    mask,
    sections,
)
from models.admin_config import ConfigPatchRequest, field_state, schema_field


def test_every_settings_field_is_classified() -> None:
    declared = {name.upper() for name in Settings.model_fields}
    assert set(FIELDS) == declared


def test_every_numeric_field_has_bounds() -> None:
    missing = [
        spec.key
        for spec in iter_fields()
        if spec.value_type in {"integer", "number"} and spec.minimum is None
    ]
    assert missing == []


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(OverrideRejected) as excinfo:
        coerce_override("NAO_EXISTE", 1)
    assert excinfo.value.code == "unknown_key"


@pytest.mark.parametrize("key", ["API_PREFIX", "ENVIRONMENT", "DATABASE_URL"])
def test_env_only_fields_refuse_override(key: str) -> None:
    with pytest.raises(OverrideRejected) as excinfo:
        coerce_override(key, "/outro")
    assert excinfo.value.code == "env_only"


def test_read_only_field_refuses_override() -> None:
    with pytest.raises(OverrideRejected) as excinfo:
        coerce_override("APP_NAME", "outro")
    assert excinfo.value.code == "read_only"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("CACHE_TTL_PROFILE_SECONDS", -1),
        ("CONCURRENCY_YOUTUBE", 0),
        ("RATELIMIT_TIKTOK_PER_MINUTE", 0),
        ("CACHE_TTL_PROFILE_SECONDS", "60"),
        ("BREAKER_ENABLED", "sim"),
        ("CACHE_BACKEND", "memcached"),
    ],
)
def test_invalid_values_are_rejected(key: str, value: object) -> None:
    with pytest.raises(OverrideRejected) as excinfo:
        coerce_override(key, value)
    assert excinfo.value.code == "invalid_value"


def test_hot_field_accepts_value_in_range() -> None:
    assert coerce_override("CACHE_TTL_PROFILE_SECONDS", 120) == 120
    assert coerce_override("BREAKER_ENABLED", True) is True


def test_override_round_trip_keeps_type() -> None:
    stored = dump_override("CACHE_TTL_POSTS_SECONDS", 300)
    assert stored == "300"
    assert load_override("CACHE_TTL_POSTS_SECONDS", stored) == 300

    stored_list = dump_override("USER_AGENTS", ["ua-1", "ua-2"])
    assert load_override("USER_AGENTS", stored_list) == ["ua-1", "ua-2"]

    stored_path = dump_override("COOKIES_DIR", "cookies")
    assert load_override("COOKIES_DIR", stored_path).name == "cookies"


def test_secret_fields_are_hot_and_never_serialized() -> None:
    for key in SECRET_KEYS:
        spec = get_field(key)
        assert spec.secret is True
        assert spec.mutability is Mutability.hot
        assert spec.editable is True
        assert mask(key, "valor-real", True) == "***"
        assert mask(key, "valor-real", False) is None
        assert schema_field(spec).default is None


def test_non_secret_field_keeps_value_in_view() -> None:
    state = field_state(get_field("CACHE_TTL_PROFILE_SECONDS"), 900, ConfigSource.env, is_set=True)
    assert state.value == 900
    assert state.secret is False
    assert state.editable is True


def test_sections_cover_every_field() -> None:
    grouped = sections()
    total = sum(len(fields) for fields in grouped.values())
    assert total == len(FIELDS)
    assert grouped["app"][0].key == "APP_NAME"


def test_patch_request_refuses_empty_body() -> None:
    with pytest.raises(ValueError):
        ConfigPatchRequest(values={})
