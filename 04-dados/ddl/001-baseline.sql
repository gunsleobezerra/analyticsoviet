-- 001 — baseline: registra o schema que já existe em produção.
-- Aplicar quando o banco ainda não tem schema_migrations.
-- Não cria tabela de dados; só o controle de versão. O baseline (1) é
-- considerado já aplicado porque src/db/session.py faz create_all no boot.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL UNIQUE,
    checksum    TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL,
    app_version TEXT
);

INSERT INTO schema_migrations (version, name, checksum, applied_at, app_version)
VALUES (1, '001-baseline: profile_snapshots', 'baseline', strftime('%Y-%m-%d %H:%M:%f', 'now'), '0.1.0')
ON CONFLICT (version) DO NOTHING;

-- Estado real hoje (referência, já criado por create_all):
-- CREATE TABLE profile_snapshots (
--     id              INTEGER PRIMARY KEY AUTOINCREMENT,
--     platform        VARCHAR(32)  NOT NULL,
--     handle          VARCHAR(128) NOT NULL,
--     captured_at     DATETIME     NOT NULL,
--     followers       INTEGER,
--     following       INTEGER,
--     posts           INTEGER,
--     likes_total     INTEGER,
--     engagement_rate FLOAT,
--     source          VARCHAR(32),
--     confidence      VARCHAR(16)
-- );
-- CREATE INDEX ix_snapshot_lookup ON profile_snapshots (platform, handle, captured_at);
