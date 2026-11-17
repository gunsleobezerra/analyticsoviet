# Modelo lógico

SGBD: SQLite (o mesmo arquivo `./data/analyticsoviet.db` do `src/core/config.py`). Tipos físicos do SQLite: `TEXT`, `INTEGER`, `BLOB`, `REAL`. Datas e horas em `TEXT` UTC; dinheiro em inteiro na menor unidade da moeda.

Convenções aplicadas em todas as tabelas:

- PK surrogate `TEXT` (UUID v4 em texto) exceto onde a tabela é de log/agregado, que usa `INTEGER PRIMARY KEY` (rowid, ordem de inserção).
- `created_at`/`updated_at` (ou equivalente nomeado) em toda tabela mutável que não é log append-only.
- FK sempre declarada, com `ON DELETE` explícito.
- Booleano = `INTEGER` com `CHECK (col IN (0,1))`.
- Nome de tabela no plural, coluna no singular, FK `<entidade>_id`.

---

## Frente A — configuração e painel

### `schema_migrations`
Uma linha por migração aplicada. Substitui a confiança cega em `create_all`.

| Coluna | Tipo | Nulo | Regra |
|---|---|---|---|
| `version` | INTEGER | não | PK |
| `name` | TEXT | não | UNIQUE, nome do arquivo |
| `checksum` | TEXT | não | sha256 do conteúdo do arquivo |
| `applied_at` | TEXT | não | instante |
| `app_version` | TEXT | sim | versão do app que aplicou |

### `config_state`
Uma linha só. Serve de sinal de invalidação entre workers: cada worker compara `version` a cada N segundos em vez de reler todas as overrides.

| Coluna | Tipo | Nulo | Regra |
|---|---|---|---|
| `id` | INTEGER | não | PK, `CHECK (id = 1)` |
| `version` | INTEGER | não | incrementado na mesma transação de qualquer escrita em `config_overrides` |
| `updated_at` | TEXT | não | instante |
| `updated_by` | TEXT | sim | FK `admin_users(id)` `ON DELETE SET NULL` |

### `config_overrides`
Valor escolhido pelo operador, por cima do ambiente.

| Coluna | Tipo | Nulo | Regra |
|---|---|---|---|
| `key` | TEXT | não | PK; nome do campo em maiúsculas (`CACHE_TTL_PROFILE_SECONDS`); `CHECK (key = upper(key))` |
| `value_json` | TEXT | não | valor serializado em JSON (`60`, `true`, `"redis://..."`, `["UA1","UA2"]`); a desserialização usa o tipo declarado no campo de `Settings` |
| `updated_at` | TEXT | não | |
| `updated_by` | TEXT | não | FK `admin_users(id)` `ON DELETE RESTRICT` |
| `updated_ip` | TEXT | sim | para diagnóstico; a fonte de verdade é a auditoria |

Sem `value_type`: o tipo é o do campo em `src/core/config.py`. `config_overrides` não aceita chave que não exista em `Settings` (validação na aplicação), e chave órfã após um rename vira aviso em `GET /admin/api/config`, nunca erro de boot.

Nenhuma coluna de segredo aqui: campo sensível é gravado em `config_secrets`, tabela separada. Motivo: `config_overrides` é lida em claro e devolvida por `GET`; se o ciphertext morasse na mesma tabela, um `SELECT *` no código de leitura de config devolveria blob no lugar de valor. `PATCH` de campo sensível grava nas duas tabelas na mesma transação (ou nenhuma).

### `config_secrets`
Campo sensível escolhido pelo operador (`META_APP_SECRET`, `S3_SECRET_ACCESS_KEY`, `S3_ACCESS_KEY_ID`, `S3_SESSION_TOKEN`, `YOUTUBE_API_KEY`). AES-256-GCM, chave `APP_ENCRYPTION_KEY` só no ambiente e `read_only` no painel.

| Coluna | Tipo | Nulo | Regra |
|---|---|---|---|
| `key` | TEXT | não | PK; mesmo formato de `config_overrides.key` |
| `ciphertext` | BLOB | não | saída do GCM |
| `nonce` | BLOB | não | 12 bytes, único por operação; `CHECK (length(nonce) = 12)` |
| `key_version` | INTEGER | não | permite rotação manual (`APP_ENCRYPTION_KEY_OLD`) sem perder segredo |
| `aad` | TEXT | não | default `'config_secret'`; `CHECK (aad = 'config_secret')` — impede aceitar ciphertext de token Meta como segredo de config |
| `updated_at` | TEXT | não | |
| `updated_by` | TEXT | não | FK `admin_users(id)` `ON DELETE RESTRICT` |
| `updated_ip` | TEXT | sim | |

AAD é a defesa contra troca de contexto: o mesmo par chave/versão decifra `meta_token` e `config_secret`, mas o texto associado diferente faz o GCM falhar. Sem `aad` gravado não há como provar isso na leitura.

### `admin_users`

| Coluna | Tipo | Nulo | Regra |
|---|---|---|---|
| `id` | TEXT | não | PK |
| `username` | TEXT | não | UNIQUE `COLLATE NOCASE` |
| `password_hash` | TEXT | não | Argon2id (`argon2-cffi`), `memory_cost=19456`, `time_cost=2`, `parallelism=1`; parâmetros ficam dentro do próprio hash codificado |
| `password_algo` | TEXT | não | `CHECK IN ('argon2id')` — fechado; bcrypt não entra |
| `must_change_password` | INTEGER | não | default 1 |
| `is_active` | INTEGER | não | default 1 |
| `password_changed_at` | TEXT | sim | |
| `created_at`, `updated_at` | TEXT | não | |

Usuário nunca é apagado com histórico: `is_active = 0`. Apagar usuário apagaria rastro de auditoria; a auditoria referencia `ON DELETE RESTRICT`.

`ADMIN_PASSWORD_HASH` do ambiente é só seed da primeira linha: troca pela tela grava em `admin_users` e revoga as sessões. Depois do seed, o banco é a fonte da verdade da senha — o ambiente não pode voltar a sobrescrever.

### `admin_sessions`
Sessão do painel. Guarda o **hash** do token do cookie, nunca o token.

| Coluna | Tipo | Nulo | Regra |
|---|---|---|---|
| `id` | TEXT | não | PK = sha256 hex do token da sessão |
| `user_id` | TEXT | não | FK `admin_users(id)` `ON DELETE CASCADE` |
| `csrf_token_hash` | TEXT | não | sha256 do token CSRF pareado com a sessão |
| `created_at` | TEXT | não | |
| `expires_at` | TEXT | não | |
| `last_seen_at` | TEXT | não | atualizado no máximo 1×/minuto para não gerar escrita por requisição |
| `revoked_at` | TEXT | sim | logout ou expiração forçada |
| `ip` | TEXT | sim | |
| `user_agent` | TEXT | sim | |

### `admin_auth_attempts`
Força bruta por IP e por login. Append-only, retenção 30 dias.

| Coluna | Tipo | Nulo | Regra |
|---|---|---|---|
| `id` | INTEGER | não | PK autoincremento |
| `username` | TEXT | sim | pode ser desconhecido |
| `ip` | TEXT | não | |
| `attempted_at` | TEXT | não | |
| `outcome` | TEXT | não | `CHECK IN ('success','bad_password','unknown_user','locked','csrf_fail')` |
| `user_agent` | TEXT | sim | |

### `config_audit_log`
Append-only. Sem UPDATE nem DELETE pela aplicação.

| Coluna | Tipo | Nulo | Regra |
|---|---|---|---|
| `id` | INTEGER | não | PK autoincremento |
| `changed_at` | TEXT | não | |
| `actor_user_id` | TEXT | não | FK `admin_users(id)` `ON DELETE RESTRICT` |
| `actor_username` | TEXT | não | cópia do login no momento (justificada abaixo) |
| `action` | TEXT | não | `CHECK IN ('set','reset','reset_section')` |
| `key` | TEXT | não | campo alterado |
| `old_value` | TEXT | sim | `NULL` = não havia override; `'***'` se sensível |
| `new_value` | TEXT | sim | `NULL` = override removido; `'***'` se sensível |
| `is_secret` | INTEGER | não | default 0 |
| `source` | TEXT | não | `CHECK IN ('panel','cli','api')` |
| `applied_mode` | TEXT | não | `CHECK IN ('hot','restart_required','rejected')` |
| `ip` | TEXT | sim | |
| `request_id` | TEXT | não | agrupa as N linhas de um mesmo PATCH ou reset de seção |

`rejected` existe para registrar tentativa recusada (ex.: valor inválido, campo sensível) sem gravar override — a #4 pede rastro do que foi tentado.

`CHECK (is_secret = 0 OR ((old_value IS NULL OR old_value = '***') AND (new_value IS NULL OR new_value = '***')))` está no DDL: a garantia de "segredo nunca em claro na auditoria" deixa de depender de o código lembrar de mascarar.

### Frente A — o que **não** vira tabela

- **Contadores de rate limit por chave de API e por conta Meta**: memória/Redis. Uma escrita por requisição no SQLite serializa o serviço inteiro (`src/db/session.py` abre uma conexão de escrita por sessão).
- **Cache de resposta**: continua no `Cache` (`src/core/cache.py`), que já tem Redis.
- **Histórico versionado de overrides para rollback** (aberto na #6): a auditoria já guarda `old_value`; um "desfazer" é reconstruível a partir dela. Tabela dedicada só se o requisito de rollback em 1 clique for confirmado — hoje não é.

---

## Frente B — chaves de API (issue #13)

### `api_keys`

| Coluna | Tipo | Nulo | Regra |
|---|---|---|---|
| `id` | TEXT | não | PK |
| `name` | TEXT | não | rótulo humano ("n8n produção") |
| `prefix` | TEXT | não | UNIQUE; `avk_` + 8 caracteres exibidos (`avk_live_ab12`); `CHECK (prefix LIKE 'avk\_%' ESCAPE '\')` |
| `key_hash` | TEXT | não | UNIQUE; sha256 hex (64 chars) do valor completo; `CHECK` de formato; busca é por aqui. SHA-256, não Argon2: o valor é 32 bytes aleatórios, não senha de humano |
| `created_at` | TEXT | não | |
| `created_by` | TEXT | sim | FK `admin_users(id)` `ON DELETE SET NULL` |
| `expires_at` | TEXT | sim | `NULL` = não expira |
| `revoked_at` | TEXT | sim | |
| `revoked_reason` | TEXT | sim | |
| `last_used_at` | TEXT | sim | atualizado no máximo 1×/minuto por chave (escrita contida, não por requisição) |

### `api_key_scopes`

| Coluna | Tipo | Nulo | Regra |
|---|---|---|---|
| `api_key_id` | TEXT | não | FK `api_keys(id)` `ON DELETE CASCADE`; PK composta |
| `scope` | TEXT | não | PK composta; `CHECK IN ('read:metrics','write:scrape','admin')` |

Escopo em tabela filha, não JSON: é conjunto consultado a cada requisição autenticada e precisa de `CHECK` por valor (1FN). Custo: um `JOIN` em tabela de dezenas de linhas.

Revogação imediata sem restart (critério da #13) é consequência de o `key_hash` ser lido do banco a cada requisição — decisão do Tech Lead: sem cache de chave. Se um dia houver cache, ele precisa de TTL ≤ 30 s ou invalidação explícita, senão o critério falha e o modelo precisa de tabela de invalidação.

Escopos e rate limit por chave: `read:metrics` → 120 req/min, `write:scrape` → 30 req/min. Os contadores **não** estão nesta tabela (ver "o que não vira tabela"). Chave sem escopo é impossível por PK composta; chave sem nenhuma linha em `api_key_scopes` é rejeitada na aplicação (não é representável como constraint).

Rotas isentas de chave (`/v1/health` para o healthcheck do compose/Dockerfile, callback OAuth, webhook da Meta) não mudam o modelo: são lista em código. `/v1/system/status` exige chave; `/mcp` fica desligado por padrão e, ligado, exige chave com escopo `admin`.

---

## Frente C — Meta

### `meta_connections`
Uma linha por autorização OAuth concluída.

| Coluna | Tipo | Nulo | Regra |
|---|---|---|---|
| `id` | TEXT | não | PK |
| `fb_user_id` | TEXT | não | id do usuário Facebook que autorizou; UNIQUE (ver nota multi-tenant) |
| `fb_user_name` | TEXT | sim | |
| `graph_version` | TEXT | não | versão usada na conexão; MetaNO confirmou **v27.0** vigente desde 04/11/2026 |
| `status` | TEXT | não | `CHECK IN ('active','revoked','expired','error')` |
| `connected_at` | TEXT | não | |
| `last_checked_at` | TEXT | sim | última verificação de token em `/me` |
| `revoked_at` | TEXT | sim | |
| `last_error` | TEXT | sim | mensagem da Meta, sem token |

### `meta_connection_scopes`
Escopos efetivamente concedidos (o que a Meta devolveu, não o que foi pedido).

| Coluna | Tipo | Nulo | Regra |
|---|---|---|---|
| `connection_id` | TEXT | não | FK `meta_connections(id)` `ON DELETE CASCADE`; PK composta |
| `scope` | TEXT | não | PK composta |
| `granted_at` | TEXT | não | |

Sem `CHECK` no escopo: a lista é da Meta e muda; comparar contra a lista esperada é papel da aplicação, e o MetaNO entrega a lista.

### `meta_assets`
O objeto da Meta sobre o qual se mede: Página, Instagram profissional ou conta de anúncios.

| Coluna | Tipo | Nulo | Regra |
|---|---|---|---|
| `id` | TEXT | não | PK |
| `connection_id` | TEXT | não | FK `meta_connections(id)` `ON DELETE RESTRICT` |
| `asset_type` | TEXT | não | `CHECK IN ('facebook_page','instagram_business','ad_account')` |
| `external_id` | TEXT | não | id na Meta; UNIQUE (`asset_type`,`external_id`) |
| `username` | TEXT | sim | handle do IG, nome curto da Página; minúsculo |
| `display_name` | TEXT | sim | |
| `parent_asset_id` | TEXT | sim | FK `meta_assets(id)` `ON DELETE SET NULL`; IG → Página; conta de anúncios não tem pai |
| `status` | TEXT | não | `CHECK IN ('active','disconnected','token_invalid','no_permission')` |
| `status_changed_at` | TEXT | não | |
| `connected_at` | TEXT | não | |
| `last_success_at` | TEXT | sim | última chamada bem-sucedida |
| `metadata_json` | TEXT | sim | campos crus que não têm coluna (moeda e fuso da conta de anúncios entram aqui se não virarem coluna) |

`status='disconnected'` é estado, não exclusão: snapshots antigos continuam apontando para o asset. `ON DELETE RESTRICT` impede apagar asset com histórico.

### `meta_tokens`
Token cifrado em repouso. Uma linha por token vigente ou histórico inválido.

| Coluna | Tipo | Nulo | Regra |
|---|---|---|---|
| `id` | TEXT | não | PK |
| `connection_id` | TEXT | não | FK `meta_connections(id)` `ON DELETE CASCADE` |
| `asset_id` | TEXT | sim | FK `meta_assets(id)` `ON DELETE CASCADE`; `NULL` = token de usuário |
| `token_kind` | TEXT | não | `CHECK IN ('user_short','user_long','page')` |
| `ciphertext` | BLOB | não | AES-GCM (escolha do Tech Lead) |
| `nonce` | BLOB | não | por token; nunca reutilizado |
| `key_version` | INTEGER | não | permite rotação da chave sem perder token |
| `issued_at` | TEXT | não | |
| `expires_at` | TEXT | sim | `NULL` = a Meta não informou expiração |
| `last_refreshed_at` | TEXT | sim | |
| `invalid_at` | TEXT | sim | quando a Meta recusou o token |
| `invalid_reason` | TEXT | sim | código de erro da Meta, sem conteúdo do token |

No máximo **um token vigente por (`connection_id`, `asset_id`, `token_kind`)**, garantido por índice único parcial `WHERE invalid_at IS NULL`. Rotação de chave = UPDATE em lote de `ciphertext`/`nonce`/`key_version`, sem tabela nova; token novo gerado pela Meta entra como linha nova e invalida a anterior na mesma transação.

### `meta_token_events`
Trilha de vida do token, append-only, para provar "detectamos a revogação" sem guardar o token.

| Coluna | Tipo | Nulo | Regra |
|---|---|---|---|
| `id` | INTEGER | não | PK autoincremento |
| `token_id` | TEXT | não | FK `meta_tokens(id)` `ON DELETE CASCADE` |
| `event` | TEXT | não | `CHECK IN ('issued','refreshed','refresh_failed','revoked_by_user','rejected_by_meta','deleted')` |
| `occurred_at` | TEXT | não | |
| `detail` | TEXT | sim | mensagem/código da Meta, nunca o token |

### `meta_webhook_events`
Recebimento e fila. A fila é esta tabela; não há broker novo (a #9 deixa aberto "banco ou Redis" — banco basta no volume assumido e sobrevive a restart).

| Coluna | Tipo | Nulo | Regra |
|---|---|---|---|
| `id` | TEXT | não | PK |
| `dedupe_key` | TEXT | não | UNIQUE; sha256(`object_type`‖`external_object_id`‖`field`‖`payload_hash`) |
| `object_type` | TEXT | não | `instagram`, `page`, ... |
| `field` | TEXT | não | `comments`, `mentions`, `feed`, `messages` |
| `external_object_id` | TEXT | sim | id do objeto que mudou |
| `asset_id` | TEXT | sim | FK `meta_assets(id)` `ON DELETE SET NULL`; preenchido quando conhecido |
| `payload_hash` | TEXT | não | sha256 do corpo |
| `signature_valid` | INTEGER | não | `CHECK IN (0,1)` |
| `status` | TEXT | não | `CHECK IN ('pending','processing','done','discarded','failed','dead')` |
| `attempts` | INTEGER | não | default 0, `CHECK (attempts >= 0)` |
| `next_attempt_at` | TEXT | sim | backoff |
| `received_at` | TEXT | não | |
| `processing_started_at` | TEXT | sim | detecta worker morto com `status='processing'` |
| `processed_at` | TEXT | sim | |
| `last_error` | TEXT | sim | |
| `raw_payload` | TEXT | sim | corpo, só enquanto necessário (ver retenção) |
| `raw_purged_at` | TEXT | sim | |

Inserção é `INSERT ... ON CONFLICT(dedupe_key) DO NOTHING`; se já existe com `status='done'`, descarta e loga. Evento com assinatura inválida **não é gravado** (critério da #9: "nada é gravado"); `signature_valid` existe para o caso de decisão futura de gravar rejeitados — hoje fica sempre 1 em linha gravada.

### `meta_usage_hourly`
Agregado por hora. Não é contador por chamada.

| Coluna | Tipo | Nulo | Regra |
|---|---|---|---|
| `id` | INTEGER | não | PK autoincremento |
| `asset_id` | TEXT | sim | FK `meta_assets(id)` `ON DELETE CASCADE`; `NULL` = uso no nível do app |
| `hour_start` | TEXT | não | início da hora UTC |
| `call_group` | TEXT | não | `CHECK IN ('graph','marketing','oauth','webhook')` |
| `calls` | INTEGER | não | default 0 |
| `errors` | INTEGER | não | default 0 |
| `retries` | INTEGER | não | default 0 |
| `breaker_opens` | INTEGER | não | default 0 |
| `quota_pct_max` | INTEGER | sim | maior percentual do cabeçalho de uso da Meta na hora, `CHECK (quota_pct_max BETWEEN 0 AND 100)` |

UNIQUE em (`COALESCE(asset_id,'')`, `hour_start`, `call_group`), com upsert. Serve `/v1/health` (janela corrente e última hora) e a série de quota.

---

## Frente D — snapshots e origem

### `profile_snapshots` (alterações)
Tabela existente; nada é removido, só acrescentado (nullable, sem default bloqueante).

| Coluna nova | Tipo | Nulo | Regra |
|---|---|---|---|
| `meta_asset_id` | TEXT | sim | FK `meta_assets(id)` `ON DELETE SET NULL` |
| `period_start` | TEXT | sim | início da janela de Insights; `NULL` = leitura pontual de perfil |
| `period_end` | TEXT | sim | |
| `inserted_by` | TEXT | sim | `CHECK IN ('request','webhook','job','migration')`; `NULL` em linha antiga = `request` |
| `dedupe_key` | TEXT | sim | UNIQUE; só webhook; `NULL` nas demais |
| `provider` | TEXT | sim | qual API produziu o dado (`meta_graph`); `NULL` = scraping. Não confundir com `source` |

`source` **não** ganha valor novo: continua o enum de `src/models/schemas.py` (`html`, `embedded_json`, `official_api`, `oembed`, `ytdlp`). Dado oficial grava `source='official_api'` + `provider='meta_graph'`, exatamente como o contrato do `ScrapeResult` (`extra.provider`). Isso evita divergência entre o enum do Pydantic e um valor que só existe no banco — divergência que a revisão apontou.

Três colunas, três perguntas distintas: `source` = tipo de fonte; `provider` = qual API; `inserted_by` = quem escreveu (requisição, webhook, job). Sem `inserted_by` não se responde "esse ponto veio de webhook duplicado ou de leitura?"; sem `provider` não se distingue Meta de YouTube Data API no futuro.

### `profile_insight_snapshots`
Métrica de período por conta conectada, formato longo.

| Coluna | Tipo | Nulo | Regra |
|---|---|---|---|
| `id` | INTEGER | não | PK autoincremento |
| `meta_asset_id` | TEXT | não | FK `meta_assets(id)` `ON DELETE CASCADE` |
| `metric` | TEXT | não | `CHECK IN ('reach','impressions','profile_views','accounts_engaged','saves','shares','follows','unfollows','website_clicks','email_contacts','net_followers')` |
| `value` | INTEGER | não | métrica ausente = **linha ausente**, nunca 0 |
| `period_start`, `period_end` | TEXT | não | |
| `granularity` | TEXT | não | `CHECK IN ('day','total_over_range','lifetime')` |
| `source` | TEXT | não | `CHECK` no enum do contrato (`official_api` no caso Meta) |
| `captured_at`, `updated_at` | TEXT | não | |

UNIQUE (`meta_asset_id`, `metric`, `period_start`, `period_end`, `granularity`) com upsert: a Meta reescreve números de período recente, e duas linhas para a mesma janela seria dupla contagem no gráfico.

`CHECK` da métrica é a lista inicial do MetaNO. Métrica nova exige migração de `CHECK` — custo aceito em troca de impedir métrica com erro de digitação. Se a lista crescer muito, vira tabela de domínio `insight_metric`; hoje é fechada e pequena.

### `meta_post_snapshots` (fase 2 da #8 — publicações)
Publicação é fato histórico com contadores que mudam; precisa de série própria.

| Coluna | Tipo | Nulo | Regra |
|---|---|---|---|
| `id` | INTEGER | não | PK autoincremento |
| `meta_asset_id` | TEXT | não | FK `meta_assets(id)` `ON DELETE CASCADE` |
| `external_post_id` | TEXT | não | UNIQUE (`meta_asset_id`,`external_post_id`,`captured_on`) |
| `permalink` | TEXT | sim | |
| `media_type` | TEXT | sim | `CHECK IN ('image','video','reel','carousel','story','unknown')` |
| `published_at` | TEXT | sim | |
| `captured_on` | TEXT | não | dia UTC da captura (`YYYY-MM-DD`), para no máximo 1 linha por dia |
| `reach`, `impressions`, `likes`, `comments`, `shares`, `saves`, `plays`, `views` | INTEGER | sim | `NULL` = não exposto pela plataforma |
| `source` | TEXT | não | `CHECK` no enum do contrato (`official_api` no caso Meta) |

### `ad_insight_snapshots` (issue #12)
Uma linha por entidade de anúncio por período.

| Coluna | Tipo | Nulo | Regra |
|---|---|---|---|
| `id` | INTEGER | não | PK autoincremento |
| `ad_account_id` | TEXT | não | FK `meta_assets(id)` `ON DELETE CASCADE` |
| `level` | TEXT | não | `CHECK IN ('account','campaign','adset','ad')` |
| `entity_external_id` | TEXT | não | id da campanha/conjunto/anúncio |
| `entity_name` | TEXT | sim | cópia do nome no momento (motivo abaixo) |
| `campaign_external_id` | TEXT | sim | `NULL` quando `level='account'` |
| `parent_external_id` | TEXT | sim | conjunto-pai, para navegar sem chamar a API |
| `status` | TEXT | sim | status no momento da captura |
| `date_start`, `date_stop` | TEXT | não | `YYYY-MM-DD`, fuso da conta |
| `breakdown_key` | TEXT | não | default `''`; vazio = sem breakdown |
| `spend_minor` | INTEGER | sim | menor unidade da moeda |
| `currency` | TEXT | sim | ISO 4217, 3 letras |
| `currency_exponent` | INTEGER | sim | 0, 2 ou 3 (o yen tem 0; o dinar kuwaitiano, 3) |
| `impressions`, `clicks`, `reach`, `results` | INTEGER | sim | |
| `frequency_x1000` | INTEGER | sim | frequência × 1000 (2,47 → 2470) |
| `ctr_x1e6` | INTEGER | sim | CTR × 1 000 000 |
| `cpc_minor`, `cpm_minor`, `cost_per_result_minor` | INTEGER | sim | |
| `result_action_type` | TEXT | sim | ex.: `onsite_conversion.messaging_conversation_started_7d` |
| `captured_at`, `updated_at` | TEXT | não | |
| `source` | TEXT | não | default `official_api`; `provider` default `meta_graph` |

UNIQUE (`ad_account_id`, `level`, `entity_external_id`, `date_start`, `date_stop`, `breakdown_key`) com upsert.

Dinheiro aqui **não** é centavo fixo: a Meta devolve decimal com a precisão da moeda da conta, e `spend` pode vir com mais casas que 2. Guardar `INTEGER` na menor unidade + `currency_exponent` evita `float` e evita perder centavo na conta de KWD/JPY. Regra do projeto respeitada (dinheiro em inteiro, nunca `REAL`); a diferença é que a unidade mínima é declarada, não presumida.

`breakdown_key` existe mesmo sem breakdown nesta entrega: acrescentar coluna à unicidade de tabela com dados é a migração mais cara do SQLite moderno (recria tabela). Vazio custa um campo e fecha a porta.

---

## Normalização, forma por forma

**1FN.** Nenhum atributo multivalorado em coluna: escopos de chave e de conexão viraram tabela filha; métricas viraram linha por métrica. `user_agents` continua lista, mas vive em `Settings`/ambiente, não em tabela.

**2FN.** Todas as PK compostas são de tabela de associação (`api_key_scopes`, `meta_connection_scopes`) e o único atributo não chave nelas (`granted_at`) depende da linha inteira. Nenhuma dependência parcial.

**3FN.** Dependências transitivas evitadas: `meta_assets` não repete dados da conexão; o vínculo IG→Página é `parent_asset_id`, não cópia do nome da Página; `config_overrides` não guarda o tipo (viria do código). Onde há transitividade, é denormalização declarada abaixo.

**BCNF.** Todo determinante é chave candidata nas tabelas de estado. Em `profile_insight_snapshots` e `ad_insight_snapshots` a unicidade composta é a chave natural e determina o resto.

### Desnormalizações declaradas (e por quê)

| Coluna | Onde | Por quê | Risco assumido |
|---|---|---|---|
| `actor_username` | `config_audit_log` | auditoria precisa mostrar quem era o login quando o usuário for renomeado ou desativado | nome pode divergir do atual; é o desejado |
| `entity_name`, `status` | `ad_insight_snapshots` | responder relatório de período sem espelhar todas as campanhas da Meta | nome é "como estava na captura" |
| `currency`, `currency_exponent` | `ad_insight_snapshots` | sem isso o valor não é interpretável; e a moeda da conta pode mudar (raro, mas acontece) | duas colunas por linha |
| `prefix` | `api_keys` | precisa ser exibível e único; derivar do hash é impossível | nenhum: prefixo é público por definição |
| `meta_assets.username` | `meta_assets` | evita varrer `metadata_json` em busca do handle para resolver URL→conta | pode ficar velho; atualizado na sincronização |
| `meta_usage_hourly.*` | agregado | contadores derivados de chamadas; materializado para não consultar log | linha é reconstruível, não fonte da verdade |

### Regras de negócio que ficam na aplicação, não em constraint

- "Campo sensível não pode ser override" — depende do conjunto em código.
- "Não gravar snapshot com todas as métricas nulas" — já existe em `src/db/snapshots.py`; manter.
- "`period_start <= period_end`" — `CHECK (period_start <= period_end)` é barato e **entra** nas tabelas de período.
- "Asset ativo pertence a conexão ativa" — depende de duas tabelas; validação na escrita, não trigger.
