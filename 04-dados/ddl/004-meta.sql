-- 004 — Meta: conexão, ativos, cofre de tokens, webhooks e uso (#10, #9, #11).

CREATE TABLE IF NOT EXISTS meta_connections (
    id               TEXT PRIMARY KEY,
    fb_user_id       TEXT NOT NULL,
    fb_user_name     TEXT,
    graph_version    TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'expired', 'error')),
    connected_at     TEXT NOT NULL,
    last_checked_at  TEXT,
    revoked_at       TEXT,
    last_error       TEXT
);
-- Uma autorização por usuário do Facebook (single-tenant). Multi-tenant:
-- incluir tenant_id aqui e na unicidade antes da primeira conta real.
CREATE UNIQUE INDEX IF NOT EXISTS ux_meta_connections_user ON meta_connections (fb_user_id);

CREATE TABLE IF NOT EXISTS meta_connection_scopes (
    connection_id  TEXT NOT NULL REFERENCES meta_connections(id) ON DELETE CASCADE,
    scope          TEXT NOT NULL,
    granted_at     TEXT NOT NULL,
    PRIMARY KEY (connection_id, scope)
);

CREATE TABLE IF NOT EXISTS meta_assets (
    id                 TEXT PRIMARY KEY,
    connection_id      TEXT NOT NULL REFERENCES meta_connections(id) ON DELETE RESTRICT,
    asset_type         TEXT NOT NULL CHECK (asset_type IN ('facebook_page', 'instagram_business', 'ad_account')),
    external_id        TEXT NOT NULL,
    username           TEXT,
    display_name       TEXT,
    parent_asset_id    TEXT REFERENCES meta_assets(id) ON DELETE SET NULL,
    status             TEXT NOT NULL CHECK (status IN ('active', 'disconnected', 'token_invalid', 'no_permission')),
    status_changed_at  TEXT NOT NULL,
    connected_at       TEXT NOT NULL,
    last_success_at    TEXT,
    metadata_json      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_meta_assets_external ON meta_assets (asset_type, external_id);
CREATE INDEX IF NOT EXISTS ix_meta_assets_connection ON meta_assets (connection_id);
CREATE INDEX IF NOT EXISTS ix_meta_assets_parent ON meta_assets (parent_asset_id);

CREATE TABLE IF NOT EXISTS meta_tokens (
    id                 TEXT PRIMARY KEY,
    connection_id      TEXT NOT NULL REFERENCES meta_connections(id) ON DELETE CASCADE,
    asset_id           TEXT REFERENCES meta_assets(id) ON DELETE CASCADE,
    token_kind         TEXT NOT NULL CHECK (token_kind IN ('user_short', 'user_long', 'page')),
    ciphertext         BLOB NOT NULL,
    nonce              BLOB NOT NULL,
    key_version        INTEGER NOT NULL,
    issued_at          TEXT NOT NULL,
    expires_at         TEXT,
    last_refreshed_at  TEXT,
    invalid_at         TEXT,
    invalid_reason     TEXT
);
-- No máximo um token vigente por (conexão, ativo, tipo).
CREATE UNIQUE INDEX IF NOT EXISTS ux_meta_tokens_current
    ON meta_tokens (connection_id, COALESCE(asset_id, ''), token_kind)
    WHERE invalid_at IS NULL;
-- Renovação: "quais tokens vencem nos próximos N dias".
CREATE INDEX IF NOT EXISTS ix_meta_tokens_expiry ON meta_tokens (expires_at)
    WHERE invalid_at IS NULL AND expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_meta_tokens_asset ON meta_tokens (asset_id);

CREATE TABLE IF NOT EXISTS meta_token_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id     TEXT NOT NULL REFERENCES meta_tokens(id) ON DELETE CASCADE,
    event        TEXT NOT NULL CHECK (event IN ('issued', 'refreshed', 'refresh_failed', 'revoked_by_user', 'rejected_by_meta', 'deleted')),
    occurred_at  TEXT NOT NULL,
    detail       TEXT
);
CREATE INDEX IF NOT EXISTS ix_meta_token_events_token ON meta_token_events (token_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS meta_webhook_events (
    id                     TEXT PRIMARY KEY,
    dedupe_key             TEXT NOT NULL,
    object_type            TEXT NOT NULL,
    field                  TEXT NOT NULL,
    external_object_id     TEXT,
    asset_id               TEXT REFERENCES meta_assets(id) ON DELETE SET NULL,
    payload_hash           TEXT NOT NULL,
    signature_valid        INTEGER NOT NULL CHECK (signature_valid IN (0, 1)),
    status                 TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'done', 'discarded', 'failed', 'dead')),
    attempts               INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at        TEXT,
    received_at            TEXT NOT NULL,
    processing_started_at  TEXT,
    processed_at           TEXT,
    last_error             TEXT,
    raw_payload            TEXT,
    raw_purged_at          TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_meta_webhook_dedupe ON meta_webhook_events (dedupe_key);
-- Consumo da fila: pendentes prontos para tentar.
CREATE INDEX IF NOT EXISTS ix_meta_webhook_queue ON meta_webhook_events (next_attempt_at)
    WHERE status = 'pending';
-- Faxina de órfãos: worker morreu com status processing.
CREATE INDEX IF NOT EXISTS ix_meta_webhook_stale ON meta_webhook_events (processing_started_at)
    WHERE status = 'processing';
CREATE INDEX IF NOT EXISTS ix_meta_webhook_asset ON meta_webhook_events (asset_id, received_at DESC);

CREATE TABLE IF NOT EXISTS meta_usage_hourly (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id        TEXT REFERENCES meta_assets(id) ON DELETE CASCADE,
    hour_start      TEXT NOT NULL,
    call_group      TEXT NOT NULL CHECK (call_group IN ('graph', 'marketing', 'oauth', 'webhook')),
    calls           INTEGER NOT NULL DEFAULT 0 CHECK (calls >= 0),
    errors          INTEGER NOT NULL DEFAULT 0 CHECK (errors >= 0),
    retries         INTEGER NOT NULL DEFAULT 0 CHECK (retries >= 0),
    breaker_opens   INTEGER NOT NULL DEFAULT 0 CHECK (breaker_opens >= 0),
    quota_pct_max   INTEGER CHECK (quota_pct_max IS NULL OR quota_pct_max BETWEEN 0 AND 100)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_meta_usage_bucket
    ON meta_usage_hourly (COALESCE(asset_id, ''), hour_start, call_group);
