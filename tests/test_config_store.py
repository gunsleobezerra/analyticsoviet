"""Tests for the persisted configuration layer.

The store is what makes a database value outrank the environment without
restarting, so these tests pin the guarantees the rest of the application leans
on: the precedence order, a write that the next read already sees, another
worker noticing the same write, a boot that survives without a database, a
secret that cannot be read or written without its key, and an audit trail that
never carries a secret in clear.
"""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select

from core.config import Settings
from core.config_crypto import ConfigCryptoError, SecretCipher
from core.config_store import (
    CacheFamily,
    ConfigDataError,
    ConfigWriteError,
    PersistentConfigStore,
    StoredConfig,
    ValueOrigin,
    sanitize_failure,
)
from db import migrate
from db.config_repository import ConfigRepository
from db.models import AdminUser, ConfigAuditLog, ConfigOverride, ConfigSecret
from db.session import Database

SECRET_FIELD = "YOUTUBE_API_KEY"


# --------------------------------------------------------------------------- #
# fixtures and helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def key() -> str:
    """A throwaway key generated in memory; never written anywhere."""
    return base64.b64encode(os.urandom(32)).decode()


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'config.db').as_posix()}"
    migrate.run_migrations(url)
    return url


async def _open_database(url: str) -> Database:
    database = Database(Settings(environment="test", database_url=url))
    await database.start()
    async with database.session() as session:
        session.add(
            AdminUser(
                id="u1",
                username="operator",
                password_hash="not-a-real-hash",
                password_algo="argon2id",
                created_at="2026-01-01 00:00:00.000000",
                updated_at="2026-01-01 00:00:00.000000",
            )
        )
        await session.commit()
    return database


@pytest.fixture
async def database(database_url: str) -> AsyncIterator[Database]:
    instance = await _open_database(database_url)
    try:
        yield instance
    finally:
        await instance.aclose()


def _store(
    database: Database,
    *,
    base: Settings | None = None,
    cipher: SecretCipher | None = None,
) -> PersistentConfigStore:
    """A store over ``database``; the reload interval is zero so tests see writes at once."""
    return PersistentConfigStore(
        base or Settings(environment="test"),
        ConfigRepository(database),
        cipher,
        reload_interval_seconds=0.0,
    )


async def _write(store: PersistentConfigStore, name: str, value: object) -> None:
    await store.set_override(name, value, actor_user_id="u1", actor_username="operator")


# --------------------------------------------------------------------------- #
# precedence: default < environment < database
# --------------------------------------------------------------------------- #


async def test_a_field_nobody_set_comes_from_the_default(database: Database) -> None:
    store = _store(database)
    effective = await store.snapshot()

    assert effective.log_level == "INFO"
    assert effective.origin_of("LOG_LEVEL") is ValueOrigin.DEFAULT


async def test_the_environment_outranks_the_default(database: Database) -> None:
    store = _store(database, base=Settings(environment="test", log_level="DEBUG"))
    effective = await store.snapshot()

    assert effective.log_level == "DEBUG"
    assert effective.origin_of("LOG_LEVEL") is ValueOrigin.ENV


async def test_the_database_outranks_the_environment(database: Database) -> None:
    store = _store(database, base=Settings(environment="test", log_level="DEBUG"))
    await _write(store, "LOG_LEVEL", "WARNING")

    effective = store.current()

    assert effective.log_level == "WARNING"
    assert effective.origin_of("LOG_LEVEL") is ValueOrigin.DATABASE


async def test_a_stored_value_survives_a_new_store(database: Database) -> None:
    await _write(_store(database), "LOG_LEVEL", "ERROR")

    effective = await _store(database).snapshot()

    assert effective.log_level == "ERROR"


async def test_reset_returns_the_field_to_the_environment(database: Database) -> None:
    store = _store(database, base=Settings(environment="test", log_level="DEBUG"))
    await _write(store, "LOG_LEVEL", "ERROR")

    effective = await store.reset("LOG_LEVEL", actor_user_id="u1", actor_username="operator")

    assert effective.log_level == "DEBUG"
    assert effective.origin_of("LOG_LEVEL") is ValueOrigin.ENV


# --------------------------------------------------------------------------- #
# visibility: a write is seen by the next read, in every worker
# --------------------------------------------------------------------------- #


async def test_a_write_is_visible_on_the_next_read(database: Database) -> None:
    store = _store(database)
    await store.snapshot()
    assert store.current().log_level == "INFO"

    await _write(store, "LOG_LEVEL", "WARNING")

    assert store.current().log_level == "WARNING"
    assert store.version == 2


async def test_another_worker_notices_the_write(database: Database) -> None:
    writer = _store(database)
    reader = _store(database)
    await reader.snapshot()

    await _write(writer, "SCRAPE_MAX_URLS", 7)
    refreshed = await reader.snapshot()

    assert refreshed.scrape_max_urls == 7
    assert refreshed.version == writer.version


# --------------------------------------------------------------------------- #
# boot without a database
# --------------------------------------------------------------------------- #


async def test_boot_without_a_database_serves_the_environment(database_url: str) -> None:
    """An unreachable database must not stop the application from starting."""
    database = Database(Settings(environment="test", database_url=database_url))
    store = _store(database, base=Settings(environment="test", log_level="DEBUG"))

    effective = await store.snapshot()

    assert effective.log_level == "DEBUG"
    assert store.degraded_reason is not None
    assert store.current() is effective


# --------------------------------------------------------------------------- #
# secrets
# --------------------------------------------------------------------------- #


def test_a_secret_round_trips(key: str) -> None:
    cipher = SecretCipher.from_setting(key)
    sealed = cipher.seal("s3cr3t", purpose="config_secret", row_id=SECRET_FIELD)

    assert sealed.ciphertext != b"s3cr3t"
    assert cipher.open(sealed, purpose="config_secret", row_id=SECRET_FIELD) == "s3cr3t"


def test_a_ciphertext_bound_to_another_field_does_not_open(key: str) -> None:
    cipher = SecretCipher.from_setting(key)
    sealed = cipher.seal("token", purpose="config_secret", row_id=SECRET_FIELD)

    with pytest.raises(ConfigCryptoError):
        cipher.open(sealed, purpose="config_secret", row_id="S3_SECRET_ACCESS_KEY")


async def test_a_secret_cannot_be_written_without_a_key(database: Database) -> None:
    store = _store(database)

    with pytest.raises(ConfigWriteError) as excinfo:
        await store.set_secret(SECRET_FIELD, "value", actor_user_id="u1", actor_username="operator")

    assert excinfo.value.code == "encryption_key_missing"


async def test_a_stored_secret_does_not_load_without_its_key(database: Database, key: str) -> None:
    writer = _store(database, cipher=SecretCipher.from_setting(key))
    await writer.set_secret(SECRET_FIELD, "abc", actor_user_id="u1", actor_username="operator")

    reader = _store(database)

    with pytest.raises(ConfigCryptoError):
        await reader.snapshot()


async def test_a_stored_secret_loads_with_its_key(database: Database, key: str) -> None:
    writer = _store(database, cipher=SecretCipher.from_setting(key))
    await writer.set_secret(SECRET_FIELD, "abc", actor_user_id="u1", actor_username="operator")

    effective = await _store(database, cipher=SecretCipher.from_setting(key)).snapshot()

    assert effective.youtube_api_key == "abc"
    assert effective.origin_of(SECRET_FIELD) is ValueOrigin.DATABASE
    shown = next(view for view in effective.views() if view.name == SECRET_FIELD)
    assert shown.value is None
    assert shown.is_set is True


# --------------------------------------------------------------------------- #
# audit trail
# --------------------------------------------------------------------------- #


async def test_a_write_records_its_audit_row(database: Database) -> None:
    store = _store(database)
    await _write(store, "LOG_LEVEL", "ERROR")

    rows = await _audit_rows(database)

    assert len(rows) == 1
    assert rows[0].action == "set"
    assert rows[0].config_key == "LOG_LEVEL"
    assert rows[0].new_value == "ERROR"
    assert rows[0].applied_mode == "hot"


async def test_the_audit_trail_never_stores_a_secret(database: Database, key: str) -> None:
    store = _store(database, cipher=SecretCipher.from_setting(key))
    await store.set_secret(SECRET_FIELD, "plain-text-secret", actor_user_id="u1", actor_username="operator")

    rows = await _audit_rows(database)
    async with database.session() as session:
        sealed = (await session.execute(select(ConfigSecret))).scalars().all()

    assert rows[0].is_secret == 1
    assert rows[0].new_value == "***"
    assert "plain-text-secret" not in f"{rows[0].old_value}{rows[0].new_value}"
    assert sealed[0].ciphertext != b"plain-text-secret"


async def _audit_rows(database: Database) -> list[ConfigAuditLog]:
    async with database.session() as session:
        return list((await session.execute(select(ConfigAuditLog))).scalars().all())


# --------------------------------------------------------------------------- #
# refusals by classification
# --------------------------------------------------------------------------- #


async def test_an_environment_only_field_is_refused(database: Database) -> None:
    store = _store(database)

    with pytest.raises(ConfigWriteError) as excinfo:
        await _write(store, "API_AUTH_ENABLED", True)

    assert excinfo.value.code == "env_only"


async def test_a_read_only_field_is_refused(database: Database) -> None:
    store = _store(database)

    with pytest.raises(ConfigWriteError) as excinfo:
        await _write(store, "APP_NAME", "outro")

    assert excinfo.value.code == "read_only"


async def test_a_secret_cannot_be_stored_as_an_override(database: Database) -> None:
    store = _store(database)

    with pytest.raises(ConfigWriteError) as excinfo:
        await _write(store, SECRET_FIELD, "in-clear")

    assert excinfo.value.code == "secret_in_overrides"


async def test_an_invalid_value_is_refused_before_the_database(database: Database) -> None:
    store = _store(database)

    with pytest.raises(ConfigWriteError) as excinfo:
        await _write(store, "HTTP_RETRIES", "not-a-number")

    assert excinfo.value.code == "invalid_value"
    assert await _audit_rows(database) == []


async def test_a_stored_environment_only_row_never_takes_effect(database: Database) -> None:
    """A row planted by hand or by an older build must not weaken the setting."""
    async with database.session() as session:
        session.add(
            ConfigOverride(
                config_key="API_AUTH_ENABLED",
                value_json="true",
                updated_at="2026-01-01 00:00:00.000000",
                updated_by="u1",
            )
        )
        await session.commit()

    effective = await _store(database).snapshot()

    assert effective.api_auth_enabled is False
    assert effective.origin_of("API_AUTH_ENABLED") is ValueOrigin.DEFAULT


async def test_an_invalid_stored_value_stops_the_resolution(database: Database) -> None:
    async with database.session() as session:
        session.add(
            ConfigOverride(
                config_key="HTTP_RETRIES",
                value_json='"not-a-number"',
                updated_at="2026-01-01 00:00:00.000000",
                updated_by="u1",
            )
        )
        await session.commit()

    with pytest.raises(ConfigDataError):
        await _store(database).snapshot()


# --------------------------------------------------------------------------- #
# cache epochs
# --------------------------------------------------------------------------- #


async def test_changing_a_ttl_bumps_only_its_family(database: Database) -> None:
    store = _store(database)
    await store.snapshot()
    before = {family: store.cache_epoch(family) for family in CacheFamily}

    await _write(store, "CACHE_TTL_PROFILE_SECONDS", 60)

    after = {family: store.cache_epoch(family) for family in CacheFamily}
    assert after[CacheFamily.PROFILE] == before[CacheFamily.PROFILE] + 1
    for family in CacheFamily:
        if family is not CacheFamily.PROFILE:
            assert after[family] == before[family]


async def test_another_worker_bumps_its_own_epoch_on_a_ttl_change(database: Database) -> None:
    writer = _store(database)
    reader = _store(database)
    await reader.snapshot()

    await _write(writer, "CACHE_TTL_POSTS_SECONDS", 10)
    await reader.snapshot()

    assert reader.cache_epoch(CacheFamily.POSTS) == 1
    assert reader.cache_epoch(CacheFamily.PROFILE) == 0


# --------------------------------------------------------------------------- #
# degradation: recovery, and what a failure is allowed to say
# --------------------------------------------------------------------------- #


class _FlakySource:
    """A source that can be told to fail, to exercise the degradation path."""

    def __init__(self, inner: ConfigRepository, error: Exception | None = None) -> None:
        self._inner = inner
        self.error = error

    async def load(self) -> StoredConfig:
        if self.error is not None:
            raise self.error
        return await self._inner.load()

    async def set_override(self, key: str, value_json: str, *, entry: object) -> int:
        return await self._inner.set_override(key, value_json, entry=entry)  # type: ignore[arg-type]

    async def set_secret(self, key: str, sealed: object, *, entry: object) -> int:
        return await self._inner.set_secret(key, sealed, entry=entry)  # type: ignore[arg-type]

    async def reset(self, key: str, *, entry: object) -> int:
        return await self._inner.reset(key, entry=entry)  # type: ignore[arg-type]


class _RecordingLogger:
    """Keeps what the store hands to the logger, field by field."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def warning(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))

    def __getattr__(self, _name: str) -> object:
        return lambda *args, **kwargs: None


async def test_a_recovered_source_clears_the_degraded_reason(database: Database) -> None:
    """The source comes back without a write, so the version does not move."""
    source = _FlakySource(ConfigRepository(database))
    store = PersistentConfigStore(
        Settings(environment="test"), source, None, reload_interval_seconds=0.0
    )

    await store.snapshot()
    assert store.degraded_reason is None

    source.error = RuntimeError("database not started")
    await store.snapshot()
    assert store.degraded_reason is not None

    source.error = None
    recovered = await store.snapshot()

    assert store.degraded_reason is None
    assert recovered.version == store.version


async def test_a_failure_reaches_the_log_without_its_password(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingLogger()
    monkeypatch.setattr("core.config_store.LOGGER", recorder)
    password = "s3nh4-que-nao-pode-vazar"
    source = _FlakySource(
        ConfigRepository(database),
        error=RuntimeError(
            f"connection to postgresql://operator:{password}@db.local:5432/config refused"
        ),
    )
    store = PersistentConfigStore(
        Settings(environment="test"), source, None, reload_interval_seconds=0.0
    )

    await store.snapshot()

    assert recorder.events, "the degradation was not logged"
    logged = " ".join(str(fields.get("reason", "")) for _, fields in recorder.events)
    assert password not in logged
    assert "db.local" in logged
    assert store.degraded_reason is not None
    assert password not in store.degraded_reason


def test_a_sensitive_query_parameter_is_masked() -> None:
    described = sanitize_failure(
        RuntimeError("connect failed: dsn?user=ops&password=hunter2&sslmode=require")
    )

    assert "hunter2" not in described
    assert "sslmode=require" in described
