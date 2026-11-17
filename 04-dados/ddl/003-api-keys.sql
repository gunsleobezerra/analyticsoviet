-- 003 — chaves de API da api pública (#13).
--
-- Decisões aplicadas: chave nomeada (não global), valor `avk_` + 32 bytes
-- aleatórios (256 bits), guardada só como SHA-256 hex + prefixo exibível.
-- Revogação vale na requisição seguinte porque a autenticação lê esta tabela
-- por requisição — não há cache de chave com TTL longo.

CREATE TABLE IF NOT EXISTS api_keys (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    prefix          TEXT NOT NULL,
    key_hash        TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    created_by      TEXT REFERENCES admin_users(id) ON DELETE SET NULL,
    expires_at      TEXT,
    revoked_at      TEXT,
    revoked_reason  TEXT,
    last_used_at    TEXT,
    CHECK (length(name) BETWEEN 1 AND 80),
    CHECK (prefix LIKE 'avk\_%' ESCAPE '\'),
    CHECK (key_hash GLOB '[0-9a-f]*' AND length(key_hash) = 64)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_api_keys_prefix ON api_keys (prefix);
-- Busca da autenticação: sha256 do valor apresentado, igualdade exata.
CREATE UNIQUE INDEX IF NOT EXISTS ux_api_keys_hash ON api_keys (key_hash);

CREATE TABLE IF NOT EXISTS api_key_scopes (
    api_key_id  TEXT NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    scope       TEXT NOT NULL CHECK (scope IN ('read:metrics', 'write:scrape', 'admin')),
    PRIMARY KEY (api_key_id, scope)
);

-- Índice parcial que evita varrer chaves revogadas na listagem do painel.
CREATE INDEX IF NOT EXISTS ix_api_keys_active ON api_keys (created_at DESC) WHERE revoked_at IS NULL;

-- Sem contador de rate limit aqui: 120/min de leitura e 30/min de scraping
-- ficam no limitador em memória/Redis (uma escrita por requisição no SQLite
-- serializa o serviço inteiro).
