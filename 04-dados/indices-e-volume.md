# Índices e volume

Premissa de volume (a confirmar com o Leonardo — é a única forma de dimensionar):

| Variável | Valor assumido |
|---|---|
| perfis acompanhados | 10 |
| leituras de perfil por dia (API + rotinas) | 300 |
| contas Meta conectadas (Página + IG) | 3 |
| conta de anúncios Meta | 1 (5 campanhas, 15 conjuntos, 40 anúncios) |
| eventos de webhook por dia | 5 000 (pico) |
| retenção de `profile_snapshots` | infinita hoje; proposta 24 meses |
| retenção de `meta_webhook_events` | 30 dias; payload cru 7 dias |

## Índice → consulta que atende

Índice sem consulta é chute. Cada linha abaixo é um índice do DDL e a consulta real que o justifica.

| Índice | Consulta que atende | Ganho |
|---|---|---|
| `ix_snapshot_lookup` (existente) | `WHERE platform=? AND handle=? AND captured_at>=? ORDER BY captured_at` — `fetch_history` em `src/db/snapshots.py` | já existe; N rows do perfil em vez de varredura |
| `ix_snapshot_asset` (parcial) | `WHERE meta_asset_id=? AND captured_at BETWEEN ? AND ?` — série de uma conta conectada | evita varredura de 100k linhas; parcial porque a maioria dos snapshots é de scraping sem asset |
| `ux_snapshot_dedupe` (parcial) | `INSERT ... ON CONFLICT(dedupe_key) DO NOTHING` no webhook | idempotência sem `SELECT` prévio |
| `ux_insight_window` | `INSERT ... ON CONFLICT(...) DO UPDATE` da janela da Meta | impede dupla contagem quando a Meta reescreve o período |
| `ix_insight_series` | `WHERE meta_asset_id=? AND metric=? ORDER BY period_start` — gráfico de alcance | cobre filtro + ordenação no mesmo índice |
| `ux_post_snapshot_day` | upsert do contador da publicação no dia | 1 linha por post por dia |
| `ix_post_snapshot_published` | "últimos posts com métrica" | ordena sem `filesort` |
| `ux_ad_insight_row` | upsert diário por entidade+período | relatório sem duplicata |
| `ix_ad_insight_period` | `WHERE ad_account_id=? AND level=? AND date_start BETWEEN ? AND ?` — relatório de campanhas | filtro por igualdade e depois faixa |
| `ix_ad_insight_campaign` | drill-down campanha → conjunto → anúncio | join pelo lado filho |
| `ux_meta_webhook_dedupe` | deduplicação na entrada | O(1) por evento |
| `ix_meta_webhook_queue` (parcial) | `WHERE status='pending' AND next_attempt_at<=now ORDER BY next_attempt_at` — worker | parcial mantém o índice pequeno com 150k linhas históricas |
| `ix_meta_webhook_stale` (parcial) | recuperação de evento preso em `processing` | idem |
| `ux_meta_tokens_current` (parcial) | "token vigente da conta" a cada chamada Graph | garante 1 vigente e serve a busca |
| `ix_meta_tokens_expiry` (parcial) | "tokens que vencem em 7 dias" para alerta em `/v1/health` | só tokens vigentes com expiração |
| `ux_api_keys_hash` | autenticação: `WHERE key_hash=?` | uma linha por requisição autenticada |
| `ix_api_keys_active` (parcial) | listagem do painel sem revogadas | índice menor |
| `ix_audit_recent` | tela de auditoria: `ORDER BY changed_at DESC LIMIT 50` | sem ordenar a tabela inteira |
| `ix_audit_key` | histórico de um campo | por campo |
| — (PK de `config_secrets`) | leitura do segredo efetivo por chave, uma vez por boot e a cada teste de conexão | PK é a própria chave |
| `ix_admin_sessions_user` | logout de todas as sessões; validação da sessão | por usuário |
| `ix_auth_attempts_ip` / `_user` | contagem de falhas na janela: `WHERE ip=? AND attempted_at>=?` | brute force em O(log n) |

Índice que **não** foi criado, de propósito:

- `profile_snapshots(source)` — cardinalidade baixa (5 valores) e o filtro sempre vem junto de plataforma/handle ou asset. Se aparecer "série só de `meta_graph`", aí entra composto `(meta_asset_id, source, captured_at)`.
- `meta_webhook_events(received_at)` — a tabela é consumida por `status`/`next_attempt_at`, não por data. Retenção usa `received_at`, mas roda 1×/dia em varredura barata.
- `meta_usage_hourly(asset_id)` isolado — a unicidade composta já começa por `COALESCE(asset_id,'')`. Atenção: como é índice por expressão, filtro `WHERE asset_id = ?` **não** o usa em SQLite; se `/v1/health` consultar por asset, criar `(asset_id, hour_start)`.

## Volume e crescimento

| Tabela | Linhas/dia | Linhas/mês | Linhas/ano | Bytes/linha (aprox.) | 1 ano |
|---|---|---|---|---|---|
| `profile_snapshots` | 300 | 9 000 | 108 000 | 150 | ~16 MB |
| `profile_insight_snapshots` | 3 contas × 11 métricas × 4 capturas = 132 | 4 000 | 48 000 | 120 | ~6 MB |
| `meta_post_snapshots` | 3 × 25 posts = 75 | 2 300 | 27 000 | 200 | ~5 MB |
| `ad_insight_snapshots` (incremental diário) | 61 | 1 800 | 22 000 | 220 | ~5 MB |
| `ad_insight_snapshots` (se recapturar 30 dias todo dia) | 1 830 | 55 000 | 660 000 | 220 | ~145 MB |
| `meta_webhook_events` | 5 000 | 150 000 | — (retenção 30 d) | 300 + payload só em falha | ~50 MB em regime |
| `meta_usage_hourly` | 4 × 24 × 4 = 384 | 11 500 | 140 000 | 80 | ~11 MB |
| `config_audit_log` | 50 | 1 500 | 18 000 | 250 | ~5 MB |
| `admin_auth_attempts` | pico 200 | 6 000 | — (retenção 30 d) | 120 | ~2 MB |
| `api_keys` | ~1 | ~20 total | ~20 | 200 | desprezível |
| `config_overrides` | — | ~50 total | ~50 | 250 | desprezível |
| `config_secrets` | — | ~8 total | ~8 | 250 + ciphertext | desprezível |

**Conclusão de volume:** o banco cresce menos de 100 MB/ano no cenário incremental. SQLite não é o gargalo de espaço; o gargalo é **escrita concorrente**.

**Regra de escrita que o modelo impõe:**

- 5 000 webhooks/dia = 0,06 escrita/s na média, mas em rajada. Cada evento grava 1 linha e depois 1-2 snapshots — transações curtas, `BEGIN IMMEDIATE`, sem `SELECT` longo dentro da transação.
- Nunca gravar contador de quota/rate limit por chamada: 3 contas × centenas de chamadas/dia × workers geraria milhares de escritas concorrentes por um dado que se perde no restart de qualquer forma.
- `last_used_at` de chave e `last_seen_at` de sessão: no máximo 1 escrita por minuto por linha (arredondar o instante antes de gravar), senão cada requisição vira uma escrita.

**Se o volume real for 10× maior** (50 contas, 50 000 webhooks/dia): o desenho continua válido, mas a migração para SQLite com `WAL` passa a ter fila de escrita. Nesse ponto a decisão é de banco (Postgres), não de tabela — e o DDL aqui é portátil com 3 ajustes (`TEXT`→`timestamptz`, `INTEGER PK`→`bigserial`, `COALESCE` em índice único por expressão → `UNIQUE NULLS NOT DISTINCT`).
