-- 005 — snapshots ganham origem oficial; métricas de período e de anúncio (#8, #12).
--
-- `source` NÃO ganha valor novo: continua o enum de src/models/schemas.py
-- (html, embedded_json, official_api, oembed, ytdlp). A origem oficial é
-- `source='official_api'` + `provider='meta_graph'`, como decidido no contrato.

-- Só ADD COLUMN nullable, sem default: SQLite aplica sem reescrever a tabela.
ALTER TABLE profile_snapshots ADD COLUMN meta_asset_id TEXT REFERENCES meta_assets(id) ON DELETE SET NULL;
ALTER TABLE profile_snapshots ADD COLUMN period_start  TEXT;
ALTER TABLE profile_snapshots ADD COLUMN period_end     TEXT;
ALTER TABLE profile_snapshots ADD COLUMN inserted_by    TEXT
    CHECK (inserted_by IS NULL OR inserted_by IN ('request', 'webhook', 'job', 'migration'));
ALTER TABLE profile_snapshots ADD COLUMN dedupe_key     TEXT;
ALTER TABLE profile_snapshots ADD COLUMN provider       TEXT;

-- Série por conta conectada: "histórico da conta X, últimos 30 dias".
CREATE INDEX IF NOT EXISTS ix_snapshot_asset
    ON profile_snapshots (meta_asset_id, captured_at)
    WHERE meta_asset_id IS NOT NULL;
-- Idempotência do webhook: mesmo evento não gera dois snapshots.
CREATE UNIQUE INDEX IF NOT EXISTS ux_snapshot_dedupe
    ON profile_snapshots (dedupe_key)
    WHERE dedupe_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS profile_insight_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    meta_asset_id  TEXT NOT NULL REFERENCES meta_assets(id) ON DELETE CASCADE,
    metric         TEXT NOT NULL CHECK (metric IN (
                       'reach', 'impressions', 'profile_views', 'accounts_engaged',
                       'saves', 'shares', 'follows', 'unfollows', 'website_clicks',
                       'email_contacts', 'net_followers')),
    value          INTEGER NOT NULL,
    period_start   TEXT NOT NULL,
    period_end     TEXT NOT NULL,
    granularity    TEXT NOT NULL CHECK (granularity IN ('day', 'total_over_range', 'lifetime')),
    source         TEXT NOT NULL CHECK (source IN ('official_api', 'html', 'embedded_json', 'oembed', 'ytdlp')),
    provider       TEXT,
    captured_at    TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    CHECK (period_start <= period_end)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_insight_window
    ON profile_insight_snapshots (meta_asset_id, metric, period_start, period_end, granularity);
-- Gráfico de uma métrica no tempo.
CREATE INDEX IF NOT EXISTS ix_insight_series
    ON profile_insight_snapshots (meta_asset_id, metric, period_start);

CREATE TABLE IF NOT EXISTS meta_post_snapshots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    meta_asset_id      TEXT NOT NULL REFERENCES meta_assets(id) ON DELETE CASCADE,
    external_post_id   TEXT NOT NULL,
    permalink          TEXT,
    media_type         TEXT CHECK (media_type IN ('image', 'video', 'reel', 'carousel', 'story', 'unknown')),
    published_at       TEXT,
    captured_on        TEXT NOT NULL,
    reach              INTEGER,
    impressions        INTEGER,
    likes              INTEGER,
    comments           INTEGER,
    shares             INTEGER,
    saves              INTEGER,
    plays              INTEGER,
    views              INTEGER,
    source             TEXT NOT NULL CHECK (source IN ('official_api', 'html', 'embedded_json', 'oembed', 'ytdlp')),
    provider           TEXT,
    inserted_by        TEXT NOT NULL DEFAULT 'job' CHECK (inserted_by IN ('request', 'webhook', 'job', 'migration'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_post_snapshot_day
    ON meta_post_snapshots (meta_asset_id, external_post_id, captured_on);
CREATE INDEX IF NOT EXISTS ix_post_snapshot_published
    ON meta_post_snapshots (meta_asset_id, published_at DESC);

CREATE TABLE IF NOT EXISTS ad_insight_snapshots (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    ad_account_id           TEXT NOT NULL REFERENCES meta_assets(id) ON DELETE CASCADE,
    level                   TEXT NOT NULL CHECK (level IN ('account', 'campaign', 'adset', 'ad')),
    entity_external_id      TEXT NOT NULL,
    entity_name             TEXT,
    campaign_external_id    TEXT,
    parent_external_id      TEXT,
    status                  TEXT,
    date_start              TEXT NOT NULL,
    date_stop               TEXT NOT NULL,
    breakdown_key           TEXT NOT NULL DEFAULT '',
    spend_minor             INTEGER CHECK (spend_minor IS NULL OR spend_minor >= 0),
    currency                TEXT CHECK (currency IS NULL OR length(currency) = 3),
    currency_exponent       INTEGER CHECK (currency_exponent IS NULL OR currency_exponent IN (0, 2, 3)),
    impressions             INTEGER,
    clicks                  INTEGER,
    reach                   INTEGER,
    results                 INTEGER,
    frequency_x1000         INTEGER,
    ctr_x1e6                INTEGER,
    cpc_minor               INTEGER,
    cpm_minor               INTEGER,
    cost_per_result_minor   INTEGER,
    result_action_type      TEXT,
    source                  TEXT NOT NULL DEFAULT 'official_api' CHECK (source IN ('official_api', 'html', 'embedded_json', 'oembed', 'ytdlp')),
    provider                TEXT NOT NULL DEFAULT 'meta_graph',
    inserted_by             TEXT NOT NULL DEFAULT 'job' CHECK (inserted_by IN ('request', 'webhook', 'job', 'migration')),
    captured_at             TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    CHECK (date_start <= date_stop),
    -- valor sem moeda não é interpretável
    CHECK (spend_minor IS NULL OR (currency IS NOT NULL AND currency_exponent IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ad_insight_row
    ON ad_insight_snapshots (ad_account_id, level, entity_external_id, date_start, date_stop, breakdown_key);
-- Relatório por período, um nível por vez.
CREATE INDEX IF NOT EXISTS ix_ad_insight_period
    ON ad_insight_snapshots (ad_account_id, level, date_start, date_stop);
CREATE INDEX IF NOT EXISTS ix_ad_insight_campaign
    ON ad_insight_snapshots (campaign_external_id, date_start);

-- Um CHECK com métrica fechada exige migração a cada métrica nova.
-- Recriar a tabela no SQLite exige: nova tabela + INSERT SELECT + DROP + RENAME
-- (12 passos do manual "ALTER TABLE" do SQLite). Se o MetaNO listar mais de
-- ~15 métricas, trocar o CHECK por tabela de domínio insight_metric.
