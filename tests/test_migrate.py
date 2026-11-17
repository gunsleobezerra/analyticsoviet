"""Tests for the schema migration runner.

The runner is the only thing that creates the schema, so these tests cover the
guarantees the rest of the system leans on: the schema it produces matches the
models, running it twice changes nothing, an edited migration is refused, and a
pre-runner database is adopted without touching its data.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from db import migrate
from db.models import Base

DOMAIN_TABLES = (
    "profile_snapshots",
    "admin_users",
    "config_state",
    "config_overrides",
    "config_secrets",
    "admin_sessions",
    "admin_auth_attempts",
    "config_audit_log",
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


def _tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row[0] for row in rows}


def _shape(connection: sqlite3.Connection, table: str) -> tuple[tuple, tuple]:
    """Columns and indexes of one table, in a form comparable across both builders.

    ``PRAGMA table_info`` reports ``notnull = 0`` for a primary key declared
    without an explicit ``NOT NULL``, while SQLAlchemy always emits ``NOT NULL``
    for a primary key column. That difference is a spelling of the same schema, so
    the primary key flag is compared and the ``notnull`` of primary key columns is
    normalised; every other column keeps its exact nullability.
    """
    columns = tuple(
        (
            row[1],  # name
            row[2],  # type
            None if row[5] else row[3],  # notnull, normalised for primary keys
            row[4],  # default
            row[5],  # pk position
        )
        for row in connection.execute(f"PRAGMA table_info({table})")
    )
    indexes = tuple(
        sorted(
            (
                row[1],  # name
                row[2],  # unique
                row[3],  # origin
                tuple(
                    info[2]
                    for info in connection.execute(f"PRAGMA index_info({row[1]})")
                ),
            )
            for row in connection.execute(f"PRAGMA index_list({table})")
        )
    )
    return columns, indexes


def _migrated_database(path: Path) -> Path:
    migrate.run_migrations(_url(path))
    return path


def _created_database(path: Path) -> Path:
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    return path


@pytest.fixture
def migrations_copy(tmp_path: Path) -> Path:
    """A writable copy of the shipped migrations, for tests that edit one."""
    target = tmp_path / "migrations"
    shutil.copytree(migrate.MIGRATIONS_DIR, target)
    return target


# --------------------------------------------------------------------------- #
# parity
# --------------------------------------------------------------------------- #


def test_migrations_and_models_produce_the_same_schema(tmp_path: Path) -> None:
    migrated = _migrated_database(tmp_path / "migrated.db")
    created = _created_database(tmp_path / "created.db")

    with _connect(migrated) as left, _connect(created) as right:
        migrated_tables = _tables(left)
        created_tables = _tables(right)

        assert set(DOMAIN_TABLES) <= migrated_tables
        assert set(DOMAIN_TABLES) <= created_tables

        for table in DOMAIN_TABLES:
            assert _shape(left, table) == _shape(right, table), (
                f"schema drift on {table}: migrations and models disagree"
            )


def test_migrations_create_every_domain_table(tmp_path: Path) -> None:
    path = _migrated_database(tmp_path / "schema.db")
    with _connect(path) as connection:
        assert set(DOMAIN_TABLES) <= _tables(connection)
        assert migrate.current_version(_url(path)) == migrate.known_version()


# --------------------------------------------------------------------------- #
# idempotency
# --------------------------------------------------------------------------- #


def test_running_twice_changes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "twice.db"

    first = migrate.run_migrations(_url(path))
    assert first.applied
    assert first.to_version == migrate.known_version()

    second = migrate.run_migrations(_url(path))
    assert second.applied == ()
    assert not second.changed
    assert second.from_version == second.to_version == migrate.known_version()

    with _connect(path) as connection:
        count = connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
    assert count == len(migrate.discover())


def test_baseline_database_is_adopted_without_touching_data(tmp_path: Path) -> None:
    """A database created before the runner has profile_snapshots but no version table."""
    path = tmp_path / "legacy.db"
    with _connect(path) as connection:
        connection.execute(
            "CREATE TABLE profile_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "platform VARCHAR(32) NOT NULL, handle VARCHAR(128) NOT NULL, "
            "captured_at DATETIME NOT NULL, followers INTEGER, following INTEGER, "
            "posts INTEGER, likes_total INTEGER, engagement_rate FLOAT, "
            "source VARCHAR(32), confidence VARCHAR(16))"
        )
        connection.execute(
            "INSERT INTO profile_snapshots (platform, handle, captured_at) "
            "VALUES ('youtube', 'kept', '2026-01-01T00:00:00Z')"
        )
        connection.commit()

    report = migrate.run_migrations(_url(path))

    assert report.baseline_stamped
    assert report.from_version == 1
    assert report.to_version == migrate.known_version()
    assert report.applied == ("002-config-admin.sql",)

    with _connect(path) as connection:
        rows = connection.execute("SELECT handle FROM profile_snapshots").fetchall()
        versions = connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    assert rows == [("kept",)]
    assert [row[0] for row in versions] == [1, 2]


# --------------------------------------------------------------------------- #
# guards
# --------------------------------------------------------------------------- #


def test_editing_an_applied_migration_aborts(tmp_path: Path, migrations_copy: Path) -> None:
    path = tmp_path / "edited.db"
    migrate.run_migrations(_url(path), directory=migrations_copy)

    target = migrations_copy / "002-config-admin.sql"
    target.write_text(target.read_text(encoding="utf-8") + "\n-- edit after apply\n", encoding="utf-8")

    with pytest.raises(migrate.MigrationError) as excinfo:
        migrate.run_migrations(_url(path), directory=migrations_copy)

    message = str(excinfo.value)
    assert "002-config-admin.sql" in message
    assert "changed after it was applied" in message


def test_database_ahead_of_the_build_is_refused(tmp_path: Path, migrations_copy: Path) -> None:
    path = tmp_path / "future.db"
    migrate.run_migrations(_url(path), directory=migrations_copy)

    (migrations_copy / "002-config-admin.sql").unlink()

    with pytest.raises(migrate.MigrationError) as excinfo:
        migrate.run_migrations(_url(path), directory=migrations_copy)

    message = str(excinfo.value)
    assert "newer schema" in message or "missing from this build" in message


def test_failed_migration_leaves_no_partial_schema(tmp_path: Path, migrations_copy: Path) -> None:
    path = tmp_path / "broken.db"
    (migrations_copy / "003-broken.sql").write_text(
        "CREATE TABLE half_applied (id INTEGER PRIMARY KEY);\n"
        "INSERT INTO does_not_exist (id) VALUES (1);\n",
        encoding="utf-8",
    )

    with pytest.raises(migrate.MigrationError) as excinfo:
        migrate.run_migrations(_url(path), directory=migrations_copy)

    assert "003-broken.sql" in str(excinfo.value)
    with _connect(path) as connection:
        assert "half_applied" not in _tables(connection)
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations")]
    assert versions == [1, 2]


def test_audit_check_rejects_a_plaintext_secret(tmp_path: Path) -> None:
    """The CHECK in the executed DDL is what keeps secrets out of the audit trail."""
    path = _migrated_database(tmp_path / "audit.db")
    with _connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO admin_users (id, username, password_hash, password_algo, "
            "created_at, updated_at) VALUES ('u1', 'operator', 'x', 'argon2id', 'now', 'now')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO config_audit_log (changed_at, actor_user_id, actor_username, "
                "action, key, new_value, is_secret, source, applied_mode, request_id) "
                "VALUES ('now', 'u1', 'operator', 'set', 'YOUTUBE_API_KEY', 'plain-value', 1, "
                "'panel', 'hot', 'req-1')"
            )
        connection.execute(
            "INSERT INTO config_audit_log (changed_at, actor_user_id, actor_username, "
            "action, key, new_value, is_secret, source, applied_mode, request_id) "
            "VALUES ('now', 'u1', 'operator', 'set', 'YOUTUBE_API_KEY', '***', 1, "
            "'panel', 'hot', 'req-1')"
        )


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def test_discovery_rejects_bad_file_names(tmp_path: Path, migrations_copy: Path) -> None:
    (migrations_copy / "3-oops.sql").write_text("SELECT 1;\n", encoding="utf-8")
    with pytest.raises(migrate.MigrationError, match=r"NNN-slug\.sql"):
        migrate.discover(migrations_copy)


def test_runner_refuses_an_in_memory_database() -> None:
    with pytest.raises(migrate.MigrationError, match="file-backed"):
        migrate.run_migrations("sqlite+aiosqlite:///:memory:")
