"""Schema migration runner.

The schema is versioned by numbered SQL files in ``db/migrations``. Each file is
applied once, inside its own ``BEGIN IMMEDIATE`` transaction, together with the
row that records it in ``schema_migrations`` — so a crash either applies a file
and its record or neither.

Design rules this module enforces:

* One file, one transaction. The bookkeeping row is written in that same
  transaction, before the commit.
* The sha256 of the file is stored. Editing a file that was already applied
  aborts with an explicit error: a schema change is a new file, never an edit.
* A database that already has ``profile_snapshots`` but no ``schema_migrations``
  is a pre-runner database. It is stamped as version 1 without touching data.
* A database whose highest version is ahead of the files shipped in this build
  is refused, because running older code against a newer schema corrupts data.
* A file lock serialises concurrent runners (two containers, or a restart in the
  middle of an upgrade).

Run it before the web server starts, never from the application lifespan:

    python -m db.migrate
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

MIGRATIONS_DIR: Final = Path(__file__).resolve().parent / "migrations"
TRACKING_TABLE: Final = "schema_migrations"
LOCK_NAME: Final = ".migrate.lock"
_FILENAME_RE: Final = re.compile(r"^(\d{3})-([a-z0-9][a-z0-9-]*)\.sql$")
_SQLITE_PREFIXES: Final = ("sqlite+aiosqlite:///", "sqlite:///")


class MigrationError(RuntimeError):
    """A migration could not be applied, or the schema history is inconsistent."""


@dataclass(frozen=True)
class Migration:
    """One numbered SQL file and its fingerprint."""

    version: int
    name: str
    path: Path
    checksum: str

    @property
    def filename(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class MigrationReport:
    """Outcome of one runner invocation."""

    database: Path
    from_version: int
    to_version: int
    applied: tuple[str, ...]
    baseline_stamped: bool

    @property
    def changed(self) -> bool:
        return bool(self.applied) or self.baseline_stamped


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def discover(directory: Path | None = None) -> list[Migration]:
    """Return every migration file, ordered by version, with its checksum."""
    root = directory or MIGRATIONS_DIR
    if not root.is_dir():
        raise MigrationError(f"migrations directory not found: {root}")

    found: dict[int, Migration] = {}
    for path in sorted(root.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if match is None:
            raise MigrationError(
                f"migration file name must be NNN-slug.sql (three digits, lowercase slug): {path.name}"
            )
        version = int(match.group(1))
        if version in found:
            raise MigrationError(
                f"duplicate migration version {version}: {found[version].filename} and {path.name}"
            )
        found[version] = Migration(
            version=version,
            name=match.group(2),
            path=path,
            checksum=_checksum(path),
        )

    if not found:
        raise MigrationError(f"no migration files in {root}")
    return [found[version] for version in sorted(found)]


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def known_version(directory: Path | None = None) -> int:
    """Highest version this build knows about."""
    return max(migration.version for migration in discover(directory))


# --------------------------------------------------------------------------- #
# database url helpers
# --------------------------------------------------------------------------- #


def database_path(database_url: str) -> Path:
    """Map a SQLAlchemy SQLite URL to the file it points at."""
    for prefix in _SQLITE_PREFIXES:
        if database_url.startswith(prefix):
            raw = database_url[len(prefix) :]
            break
    else:
        raise MigrationError(
            f"only SQLite URLs are supported by this runner, got: {_redact(database_url)}"
        )

    if not raw or raw == ":memory:":
        raise MigrationError("the migration runner needs a file-backed database, not :memory:")
    return Path(raw).resolve()


def _redact(database_url: str) -> str:
    """Drop any ``user:password@`` segment before a URL reaches a log line."""
    return re.sub(r"//[^/@]*@", "//***@", database_url)


# --------------------------------------------------------------------------- #
# file lock
# --------------------------------------------------------------------------- #


@contextmanager
def _exclusive_lock(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive OS-level lock on ``lock_path`` for the block's duration.

    ``flock`` on POSIX and ``msvcrt.locking`` on Windows: the lock dies with the
    process, so a crashed migration never leaves a stale lock behind.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")  # noqa: SIM115 - released in finally
    try:
        try:
            _lock(handle)
        except OSError as exc:  # pragma: no cover - depends on a second process
            raise MigrationError(
                f"another migration is running (lock held on {lock_path}); "
                "wait for it to finish and run again"
            ) from exc
        yield
    finally:
        try:
            _unlock(handle)
        finally:
            handle.close()


def _lock(handle: object) -> None:
    if os.name == "nt":  # pragma: no cover - platform specific
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
    else:  # pragma: no cover - platform specific
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]


def _unlock(handle: object) -> None:
    if os.name == "nt":  # pragma: no cover - platform specific
        import msvcrt

        handle.seek(0)  # type: ignore[attr-defined]
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
    else:  # pragma: no cover - platform specific
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# sql helpers
# --------------------------------------------------------------------------- #


def _statements(sql: str) -> list[str]:
    """Split a SQL script into complete statements.

    ``sqlite3.complete_statement`` knows about comments and quoted text, so this
    never cuts a statement in half at a semicolon inside a string.
    """
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer)
            buffer = ""
    if buffer.strip():
        raise MigrationError(f"incomplete SQL statement at end of script: {buffer.strip()[:80]!r}")
    return statements


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _applied(connection: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    rows = connection.execute(
        f"SELECT version, name, checksum FROM {TRACKING_TABLE} ORDER BY version"
    ).fetchall()
    return {int(version): (str(name), str(checksum)) for version, name, checksum in rows}


def _configure(connection: sqlite3.Connection) -> None:
    """Per-connection pragmas the migration itself relies on."""
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA synchronous=NORMAL")


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #


def run_migrations(
    database_url: str,
    *,
    app_version: str | None = None,
    directory: Path | None = None,
) -> MigrationReport:
    """Bring the database up to the highest migration version shipped here.

    Returns the report of what changed. Raises :class:`MigrationError` when the
    history is inconsistent, a checksum diverges, or the database is newer than
    this build.
    """
    migrations = discover(directory)
    path = database_path(database_url)
    path.parent.mkdir(parents=True, exist_ok=True)
    version_of_build = migrations[-1].version
    stamp = app_version or _package_version()

    lock_path = path.parent / LOCK_NAME
    with _exclusive_lock(lock_path):
        connection = sqlite3.connect(path, isolation_level=None, timeout=5.0)
        try:
            _configure(connection)
            # WAL is a property of the database file, not of the connection, so
            # it is set once here and never inside a transaction.
            connection.execute("PRAGMA journal_mode=WAL")

            baseline_stamped = False
            if not _has_table(connection, TRACKING_TABLE):
                if _has_table(connection, "profile_snapshots"):
                    baseline_stamped = _stamp_baseline(connection, migrations[0], stamp)
                else:
                    baseline_stamped = False

            applied_rows = (
                _applied(connection) if _has_table(connection, TRACKING_TABLE) else {}
            )
            _verify_history(applied_rows, migrations, version_of_build)

            from_version = max(applied_rows, default=0)
            applied: list[str] = []
            for migration in migrations:
                if migration.version in applied_rows:
                    continue
                _apply(connection, migration, stamp)
                applied.append(migration.filename)

            to_version = max((m.version for m in migrations if m.filename in applied), default=from_version)
            if baseline_stamped:
                to_version = max(to_version, 1)
            return MigrationReport(
                database=path,
                from_version=from_version,
                to_version=to_version,
                applied=tuple(applied),
                baseline_stamped=baseline_stamped,
            )
        finally:
            connection.close()


_TRACKING_DDL: Final = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    " version INTEGER PRIMARY KEY,"
    " name TEXT NOT NULL UNIQUE,"
    " checksum TEXT NOT NULL,"
    " applied_at TEXT NOT NULL,"
    " app_version TEXT)"
)


def _stamp_baseline(connection: sqlite3.Connection, baseline: Migration, app_version: str) -> bool:
    """Record an existing pre-runner database as version 1 without touching data.

    Only the bookkeeping table is created here — the domain schema is already in
    place, which is exactly what made this database a baseline. The file itself
    is not executed, and its checksum is recorded so later edits still abort.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(_TRACKING_DDL)
        _record(connection, baseline, app_version)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return True


def _apply(connection: sqlite3.Connection, migration: Migration, app_version: str) -> None:
    """Run one migration and record it in the same transaction."""
    statements = _statements(migration.path.read_text(encoding="utf-8"))
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in statements:
            connection.execute(statement)
        _record(connection, migration, app_version)
        connection.execute("COMMIT")
    except Exception as exc:
        connection.execute("ROLLBACK")
        raise MigrationError(
            f"migration {migration.filename} failed and was rolled back: {exc}"
        ) from exc


def _record(connection: sqlite3.Connection, migration: Migration, app_version: str) -> None:
    connection.execute(
        f"INSERT INTO {TRACKING_TABLE} (version, name, checksum, applied_at, app_version) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            migration.version,
            migration.name,
            migration.checksum,
            datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            app_version,
        ),
    )


def _verify_history(
    applied_rows: dict[int, tuple[str, str]],
    migrations: Sequence[Migration],
    version_of_build: int,
) -> None:
    """Refuse to run against an edited file or a database ahead of this build."""
    if not applied_rows:
        return

    highest = max(applied_rows)
    if highest > version_of_build:
        raise MigrationError(
            f"database is at schema version {highest} but this build only knows {version_of_build}; "
            "refusing to start against a newer schema — deploy the matching version instead"
        )

    by_version = {migration.version: migration for migration in migrations}
    for version in sorted(applied_rows):
        applied_name, applied_checksum = applied_rows[version]
        migration = by_version.get(version)
        if migration is None:
            raise MigrationError(
                f"database has migration {version} ({applied_name}) that is missing from this build; "
                "the migration files and the database are out of sync"
            )
        if migration.checksum != applied_checksum:
            raise MigrationError(
                f"migration {migration.filename} changed after it was applied "
                f"(recorded checksum {applied_checksum[:12]}…, file checksum {migration.checksum[:12]}…); "
                "restore the file and add a new migration instead of editing an applied one"
            )


def current_version(database_url: str) -> int:
    """Highest version recorded in the database, or 0 when the schema is unmanaged."""
    path = database_path(database_url)
    if not path.exists():
        return 0
    connection = sqlite3.connect(path, isolation_level=None, timeout=5.0)
    try:
        if not _has_table(connection, TRACKING_TABLE):
            return 0
        row = connection.execute(f"SELECT max(version) FROM {TRACKING_TABLE}").fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    finally:
        connection.close()


def _package_version() -> str:
    try:
        from core.config import get_settings

        return get_settings().app_version
    except Exception:  # pragma: no cover - settings are optional for the runner
        return "unknown"


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m db.migrate [--database-url URL]``."""
    args = list(sys.argv[1:] if argv is None else argv)
    database_url: str | None = None
    if args and args[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    if args and args[0] == "--database-url":
        if len(args) < 2:
            print("--database-url needs a value", file=sys.stderr)
            return 2
        database_url = args[1]
        args = args[2:]
    if args:
        print(f"unexpected arguments: {' '.join(args)}", file=sys.stderr)
        return 2

    if database_url is None:
        from core.config import get_settings

        database_url = get_settings().database_url

    try:
        report = run_migrations(database_url)
    except MigrationError as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1

    if not report.changed:
        print(f"schema up to date at version {report.to_version} ({report.database})")
        return 0
    stamped = " (baseline stamped)" if report.baseline_stamped else ""
    print(
        f"schema {report.from_version} -> {report.to_version}{stamped}; "
        f"applied: {', '.join(report.applied) or 'none'}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
