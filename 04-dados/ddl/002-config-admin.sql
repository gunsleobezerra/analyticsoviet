-- 002 — configuração persistida, painel e auditoria (#6, #4, #3).

CREATE TABLE IF NOT EXISTS config_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    version     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT    NOT NULL,
    updated_by  TEXT    REFERENCES admin_users(id) ON DELETE SET NULL
);

INSERT INTO config_state (id, version, updated_at) VALUES (1, 1, strftime('%Y-%m-%d %H:%M:%f', 'now'))
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS admin_users (
    id                    TEXT    PRIMARY KEY,
    username              TEXT    NOT NULL COLLATE NOCASE,
    password_hash         TEXT    NOT NULL,
    password_algo         TEXT    NOT NULL CHECK (password_algo IN ('argon2id', 'bcrypt')),
    must_change_password  INTEGER NOT NULL DEFAULT 1 CHECK (must_change_password IN (0, 1)),
    is_active             INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    password_changed_at   TEXT,
    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_admin_users_username ON admin_users (username);

CREATE TABLE IF NOT EXISTS config_overrides (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT NOT NULL REFERENCES admin_users(id) ON DELETE RESTRICT,
    updated_ip  TEXT,
    CHECK (key = upper(key)),
    CHECK (length(key) BETWEEN 1 AND 64)
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    id               TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    csrf_token_hash  TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    expires_at       TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    revoked_at       TEXT,
    ip               TEXT,
    user_agent       TEXT,
    CHECK (expires_at > created_at)
);
CREATE INDEX IF NOT EXISTS ix_admin_sessions_user ON admin_sessions (user_id, expires_at);

CREATE TABLE IF NOT EXISTS admin_auth_attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT,
    ip            TEXT NOT NULL,
    attempted_at  TEXT NOT NULL,
    outcome       TEXT NOT NULL CHECK (outcome IN ('success', 'bad_password', 'unknown_user', 'locked', 'csrf_fail')),
    user_agent    TEXT
);
CREATE INDEX IF NOT EXISTS ix_auth_attempts_ip ON admin_auth_attempts (ip, attempted_at);
CREATE INDEX IF NOT EXISTS ix_auth_attempts_user ON admin_auth_attempts (username, attempted_at);

CREATE TABLE IF NOT EXISTS config_audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at      TEXT    NOT NULL,
    actor_user_id   TEXT    NOT NULL REFERENCES admin_users(id) ON DELETE RESTRICT,
    actor_username  TEXT    NOT NULL,
    action          TEXT    NOT NULL CHECK (action IN ('set', 'reset', 'reset_section')),
    key             TEXT    NOT NULL,
    old_value       TEXT,
    new_value       TEXT,
    is_secret       INTEGER NOT NULL DEFAULT 0 CHECK (is_secret IN (0, 1)),
    source          TEXT    NOT NULL CHECK (source IN ('panel', 'cli', 'api')),
    applied_mode    TEXT    NOT NULL CHECK (applied_mode IN ('hot', 'restart_required', 'rejected')),
    ip              TEXT,
    request_id      TEXT    NOT NULL
);
-- Tela de auditoria: últimas N alterações.
CREATE INDEX IF NOT EXISTS ix_audit_recent ON config_audit_log (changed_at DESC);
-- Histórico de um campo específico.
CREATE INDEX IF NOT EXISTS ix_audit_key ON config_audit_log (key, changed_at DESC);
-- Todas as linhas de uma mesma escrita.
CREATE INDEX IF NOT EXISTS ix_audit_request ON config_audit_log (request_id);
