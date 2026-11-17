-- 001 — baseline: the schema that existed before the runner, plus its bookkeeping.
--
-- A database that already has profile_snapshots is stamped with this version
-- without executing the file, so existing data is never touched. This file is
-- what a brand-new database runs: it creates the same history table the
-- application used to create at boot, and the version table itself.

CREATE TABLE IF NOT EXISTS profile_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        VARCHAR(32)  NOT NULL,
    handle          VARCHAR(128) NOT NULL,
    captured_at     DATETIME     NOT NULL,
    followers       INTEGER,
    following       INTEGER,
    posts           INTEGER,
    likes_total     INTEGER,
    engagement_rate FLOAT,
    source          VARCHAR(32),
    confidence      VARCHAR(16)
);
CREATE INDEX IF NOT EXISTS ix_snapshot_lookup ON profile_snapshots (platform, handle, captured_at);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL UNIQUE,
    checksum    TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL,
    app_version TEXT
);
