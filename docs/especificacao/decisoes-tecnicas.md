# Decisões técnicas — Tech Lead (painel /admin + integração Meta)

Data: 2026-11-17. Base: clone em `/var/lib/openbot/OpenBot/Shared/analyticsoviet`, `main` @ `d1c42dc`.
Escopo: fechar o que o parecer do Revisor atribuiu ao Tech Lead — **frontend, hash de senha, criptografia, versionamento de schema** — e o que **não** construir.
Não cobre fatos da Meta (versão da Graph, permissões, quota, webhook): isso é do MetaNO.

## Restrições que vêm do código

- `Dockerfile` instala só `pyproject.toml` e copia `src/`. Não existe Node, bundler nem etapa de build. Adicionar SPA = build multi-stage, `node_modules`, lockfile npm e imagem maior.
- `src/api/docs.py` já serve UI sem CDN, com allowlist de arquivo e `FileResponse`. É o padrão de asset do projeto e não precisa de nada novo.
- `src/main.py` monta `mcp.sse_app` em `/mcp` com `allowed_origins=["*"]`; `src/mcp_server.py` chama o scraping direto, fora de `/v1`.
- `src/db/session.py` roda `Base.metadata.create_all` no `lifespan`, que **não altera tabela existente**. A entrega tem `ALTER TABLE` (5 colunas em `profile_snapshots`) e 10+ tabelas novas.
- `src/models/schemas.py`: `Source` tem 5 valores (`html`, `embedded_json`, `official_api`, `oembed`, `ytdlp`); `Profile` e `Post` têm `extra: dict`; `ScrapeResult` **não** tem `extra`.
- `src/core/cache.py` expõe só `get/set/delete/close/stats` — não há como varrer chave.
- `httpx.AsyncClient` é construído uma vez em `start()` com proxy/timeout/limits fixos.

## Resumo

| # | Item | Decisão | Alternativa descartada |
|---|---|---|---|
| 1 | Frontend do painel | HTML estático + ES modules, sem framework e sem build; assets em `src/api/static/admin/` servidos no padrão de `docs.py` | React/Vue/Svelte com Node; Jinja2 + htmx |
| 2 | Hash de senha | `argon2-cffi`, Argon2id, hash **no banco** (`admin_users`), ambiente só para o seed inicial | `passlib[bcrypt]`; hash só no ambiente |
| 3 | Criptografia de segredo/token | AES-256-GCM (`cryptography`), chave única `APP_ENCRYPTION_KEY` no ambiente, `key_version` + AAD por finalidade | Fernet; segredo só no `.env`; SQLCipher |
| 4 | Versionamento de schema | Runner próprio, SQL numerado em `src/db/migrations/`, `schema_migrations` com checksum, executado antes do uvicorn | Alembic (fica como escape, ver §4.4) |
| 5 | O que não construir | Lista fechada em §5 | — |

Dependências novas em `pyproject.toml`: `argon2-cffi>=23.1`, `cryptography>=43`. Nenhuma dependência de frontend.

---

## 1. Frontend do painel: HTML estático + JS puro, sem build

**Decisão.** `/admin` é uma página estática (`admin.html`, `admin.js` em ES2020, `admin.css`) em `src/api/static/admin/`, servida por allowlist explícita (`FileResponse` + `media_type`, igual a `_ASSET_MEDIA_TYPES` em `src/api/docs.py`). O formulário é montado no cliente a partir de `GET /admin/api/config/schema`, como a #7 já exige. Nenhum HTML é gerado no servidor com o valor de configuração — só JSON.

**Por quê.**
- O painel é formulário: ~50 campos, um operador, uma sessão, sem tempo real, sem rota cliente. O que um framework entrega (roteamento, estado, componentes) não é usado; o que ele cobra (build, Node na imagem, supply chain npm, CSP mais frouxa) é cobrado inteiro.
- O `Dockerfile` atual não tem Node. Uma SPA obriga a mudar o build, manter `package-lock.json` e auditar dependências — custo permanente para um ganho de zero.
- O padrão sem CDN de `docs.py` já está provado em produção e cabe na allowlist de 3 arquivos.
- Jinja2 também foi descartado: o shell não tem nada dinâmico além do token CSRF, que chega por `GET /admin/api/session`. Adicionar template engine para isso é superfície sem retorno.

**Consequências e limites.**
- Orçamento: `admin.js` ≤ 400 linhas, `admin.css` ≤ 200; sem transpile, sem polyfill, alvo = navegador atual do operador (Chrome/Firefox/Safari ≥ 2023).
- Sem framework de UI e sem biblioteca de gráfico nesta entrega. Se aparecer gráfico depois, avaliar `uPlot` vendorizado (≈40 KB, um arquivo) — decisão nova, não agora.
- Acessibilidade mínima obrigatória: `<label for>` em todo campo, `aria-describedby` no erro, `aria-live="polite"` na faixa de status, foco no primeiro campo inválido. Isso entra nos critérios de aceite da #7, não é opcional.
- CSP `default-src 'self'` é possível justamente por não haver CDN; a implementação deve enviá-la em `/admin`.
- Teste: `pytest` verifica que `/admin` responde 200 com `text/html`, que os 3 assets são servidos com o `content-type` certo, que qualquer outro nome dá 404, e que nenhuma URL externa aparece no HTML/JS (grep no teste).

## 2. Senha do painel: Argon2id via `argon2-cffi`, hash no banco

**Decisão.** `argon2-cffi` com `PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, hash_len=32, salt_len=16)` — Argon2id, baseline do OWASP (19 MiB). O hash vive em `admin_users.password_hash` (a tabela que o Modelador já desenhou), com `password_algo='argon2id'`. `ADMIN_PASSWORD_HASH` no ambiente serve **só de seed** quando não existe usuário ativo; depois disso, trocar a senha é escrever no banco pela tela.

**Por quê.**
- `passlib` está sem release desde 2020 e quebra com `bcrypt>=4.1` (lê `bcrypt.__about__`); não entra em projeto novo. `argon2-cffi` é o binding de referência, tem wheel manylinux para cp312, não exige compilador na imagem e expõe `check_needs_rehash()` para subir parâmetro sem migração.
- Hash no banco, e não no ambiente, resolve o bloqueio 7 do revisor: "senha inicial via ambiente, trocada na tela" **exige** armazenar o hash novo em algum lugar. Um hash Argon2id não é segredo na mesma classe de um token — é verificador de mão única, e o custo de 19 MiB por tentativa torna ataque offline caro. Sem isso, "troca obrigatória no primeiro acesso" sai do escopo.
- Um usuário só (`ADMIN_USERNAME`, default `admin`), sem CRUD de usuário. A tabela suporta N; a UI não expõe N.

**Consequências.**
- Verificação roda em `starlette.concurrency.run_in_threadpool`: ~30–60 ms de CPU e 19 MiB por login bloqueiam o event loop se chamados direto.
- Usuário inexistente também verifica contra um hash dummy (custo igual), para não vazar existência por tempo de resposta.
- `check_needs_rehash` no login bem-sucedido: se os parâmetros mudarem no futuro, o hash é regravado na hora.
- Troca de senha pela tela grava `password_hash`, `password_changed_at` e revoga **todas** as sessões, inclusive a atual (obriga novo login).
- CLI de hash: `python -m tools.admin password-hash` lê a senha do stdin, nunca de `argv` (argv aparece em `ps` e no histórico do shell). Sem senha em log, em auditoria ou em resposta.
- Sessão: cookie `HttpOnly` + `SameSite=Lax` + `Secure` quando `environment=prod` e `public_base_url` https; TTL absoluto de 12 h (`admin_session_ttl_seconds=43200`), sem renovação deslizante; CSRF por header `X-CSRF-Token` obtido em `GET /admin/api/session`.
- Força bruta: 5 falhas por IP em 15 min → bloqueio de 15 min, registrado em `admin_auth_attempts`. Contador em memória (o SQLite não recebe uma escrita por tentativa).

## 3. Criptografia de segredo e de token: AES-256-GCM, uma chave

**Decisão.** `cryptography` `AESGCM`, chave de 32 bytes em `APP_ENCRYPTION_KEY` (base64), **somente ambiente e `read_only` no painel**. Cada cifra: nonce de 12 bytes de `os.urandom`, `key_version` inteiro gravado na linha, AAD = `"{finalidade}:{id_da_linha}:{key_version}"`, com `finalidade ∈ {meta_token, config_secret}`.

**Por quê.**
- GCM é AEAD: confidencialidade e integridade num passo; ciphertext adulterado falha na decifra, não vira dado silenciosamente errado.
- AAD amarra o ciphertext ao lugar: um token copiado para a linha de um segredo de config (ou de outra conta) não decifra. Sem AAD essa troca passa.
- `Fernet` (AES-128-CBC + HMAC) é mais simples, mas não tem AAD, limita a 128 bits e não ganha nada aqui.
- **Uma chave, não duas.** O arquiteto propôs `CONFIG_SECRET_KEY` e `META_TOKEN_ENCRYPTION_KEY`; duas chaves significam dois procedimentos de rotação, duas variáveis para esquecer e nenhum ganho real de isolamento — quem lê o ambiente lê as duas. Fica `APP_ENCRYPTION_KEY`, com finalidade no AAD.
- Isso **fecha o conflito 6 do revisor a favor do arquiteto**: segredo de config é gravado cifrado no banco. Motivo de produto: o pedido do Leonardo é configurar pela tela o que hoje só vive no `.env`; sem isso, `S3_SECRET_ACCESS_KEY`, `YOUTUBE_API_KEY` e `META_APP_SECRET` continuam exigindo editar arquivo e reiniciar. O risco que o Modelador levanta (`.db` copiado + chave no ambiente) é real, mas o estado atual é pior: segredo em texto puro no `.env` ao lado do mesmo `.db`.
- Forma: tabela própria `config_secrets` (`key`, `ciphertext`, `nonce`, `key_version`, `updated_at`, `updated_by`), não colunas nullable em `config_overrides`. Motivo: `config_overrides` tem `value_json NOT NULL` e CHECK de chave; misturar claro e cifrado na mesma linha obriga todo `SELECT` a perguntar qual é qual. A forma final é do Modelador; o requisito é "linha separada, `key_version` na linha".

**Consequências.**
- Sem `APP_ENCRYPTION_KEY`: escrita de segredo no painel é recusada com 422 ("defina APP_ENCRYPTION_KEY no ambiente"); leitura mostra `is_set: true, value: null` para o que veio do `.env`. Boot com linha cifrada e chave ausente **falha com mensagem clara** — não sobe servindo token quebrado.
- Perder a chave inutiliza os segredos (recadastrar), não os dados históricos. Está escrito no README do deploy.
- Rotação: `APP_ENCRYPTION_KEY` (versão N) + `APP_ENCRYPTION_KEY_OLD` (N-1, decifra e não cifra) e um comando `python -m tools.crypto rotate` que reescreve as linhas em transação com `key_version` novo. Não há rotação automática nem agendada nesta entrega.
- Nada de segredo em log, auditoria, resposta HTTP, `/v1/health` ou mensagem de erro. Auditoria de segredo grava `"alterado"` + tamanho, nunca valor nem hash.
- Comparação de chave de API é `secrets.compare_digest`; nonce nunca reutilizado (12 bytes aleatórios por operação, gravados na linha).

## 4. Versionamento de schema: runner próprio em SQL, com escape para Alembic

**Decisão.** Runner próprio: arquivos `src/db/migrations/NNN-nome.sql` (numerados, um assunto por arquivo), controlados por `schema_migrations` (`version` PK, `name`, `checksum` sha256, `applied_at`, `app_version`). Executado **antes** do uvicorn no `CMD`/entrypoint, nunca no `lifespan`. `Base.metadata.create_all` sai do caminho de produção.

**Por quê.**
- As migrações desta entrega são `CREATE TABLE` e `ALTER TABLE ... ADD COLUMN` nullable: SQL direto, legível e revisável. O runner cabe em ~80 linhas e casa com o estilo do projeto (asset embutido, sem dependência de terceiro).
- Alembic resolveria, mas cobra `alembic.ini`, `env.py`, diretório `versions/`, autogenerate ruidoso em SQLite (exige `render_as_batch`, não detecta várias mudanças) e a separação sync/async — superfície desproporcional para 6 arquivos que já estão escritos e validados.
- **Não é porta de mão única**: adotar Alembic depois é `alembic init` + uma revisão baseline equivalente ao schema atual + `alembic stamp head`. Custa uma hora, não uma reescrita.

**Regras não negociáveis (viram critério de aceite).**
1. Um arquivo = uma transação `BEGIN IMMEDIATE`; a linha em `schema_migrations` entra **na mesma transação**, antes do `COMMIT`.
2. `checksum` sha256 do arquivo: arquivo aplicado e depois editado **aborta o boot** com mensagem explícita. Migração nova é arquivo novo, nunca edição de aplicado.
3. Banco existente com `profile_snapshots` e sem `schema_migrations` = baseline: registra a versão 1 sem executar DDL, sem tocar em dados.
4. `PRAGMA journal_mode=WAL` uma vez, fora de transação; `foreign_keys=ON`, `busy_timeout=5000` e `synchronous=NORMAL` por conexão, em listener de `src/db/session.py`. Sem o listener, toda FK do DDL é decoração.
5. Trava de arquivo (`flock` em `data/.migrate.lock`) — dois containers ou um restart durante a migração não podem aplicar o mesmo arquivo duas vezes.
6. Guarda de downgrade: se `max(version)` do banco > versão conhecida pelo código, o processo **não sobe**. Rodar código antigo contra schema novo é a forma mais barata de corromper dado.
7. Os `.sql` vivem no pacote (`src/db/migrations/`), não em `04-dados/`: o container copia `src/` e só isso. O DDL de referência do Modelador é a origem; o arquivo empacotado é a verdade executada.
8. Teste de paridade: um banco criado pelas migrações e outro por `create_all` têm de produzir o mesmo `PRAGMA table_info` e `index_list`. Sem esse teste, DDL e modelos divergem no primeiro rename.
9. Teste de idempotência: rodar o runner duas vezes seguidas não altera nada e não falha.

**Quando trocar para Alembic.** Na primeira migração que exigir recriar tabela (mudar UNIQUE/PK, coluna NOT NULL, `tenant_id` se um dia existir) ou transformar dado com FK. O runner próprio não faz `create-copy-drop-rename` com segurança; aí Alembic `batch_alter_table` paga o custo.

## 5. O que NÃO construir (lista fechada)

Cada item tem motivo; "depois" significa que precisa de requisito novo, não de tempo.

**Painel e configuração**
- SPA/framework/Node, CSS de framework, fonte remota, CDN, tema escuro, i18n. — Sem requisito; quebra o build atual.
- Multi-usuário, RBAC, 2FA/TOTP, reset de senha por e-mail, convite de usuário. — Um operador; cada item é superfície de autenticação sem uso.
- Aplicação a quente de campo frio (`database_url`, `cache_backend`, `redis_url`, cookies/S3, `public_base_url`, `proxy_url`, timeouts). — Ressalva: continuam com selo "exige reinício"; nada de recriar `HttpClient` nem de aplicar em silêncio.
- Editor de histórico/rollback de configuração em 1 clique. — A auditoria guarda `old_value`/`new_value`; reconstruir é manual de propósito.
- Barramento de config (pub/sub Redis obrigatório, websocket de config). — Versão no banco + poll ≤ 1 s resolve com `CACHE_BACKEND=memory`.
- Validador de campo customizado por tela. — A validação é o pydantic de `Settings`; a tela só exibe o 422.
- Teste de conexão que escreve (PUT no bucket, envio real de mensagem). — Só leitura/`HEAD`/`PING`/chamada barata; escrita de teste altera dado de terceiro.
- Endpoint que devolve segredo em claro, "olho mágico", exportar `.env`. — Não existe versão segura disso.

**Autenticação da API (#13)**
- OAuth2/JWT para terceiros, refresh token, endpoint de token. — Chave de API resolve o caso real (n8n/Make/scripts); JWT é outro produto.
- IP allowlist por chave. — Depois, e só com requisito; hoje o proxy não garante o IP de origem.
- Escopo além de `read:metrics`, `write:scrape`, `admin`, ou escopo por rota granular. — Três valores cobrem o contrato atual.
- Chave "global única". — Chaves nomeadas, uma por integração: revogar uma não derruba as outras.
- Autenticação em `/v1/health` (quebra healthcheck do compose e do Dockerfile) e em `POST /v1/meta/webhook` / `GET /v1/meta/oauth/callback` (quem chama não tem chave).
- `/mcp` aberto. — Fica `MCP_ENABLED=false` por padrão; quando ligado, exige a mesma chave de API (a rota chama o scraping direto em `src/mcp_server.py`, então a proteção de `/v1` não a cobre).

**Meta**
- Multi-tenant (`tenant_id`, tabela de tenant, isolamento por cliente). — Single-tenant nesta entrega; com uma conta real e ~5 linhas, a migração depois é trivial. Adicionar coluna especulativa não.
- Escrita na conta de anúncios (pausar, ajustar orçamento, criar público). — Só `GET`; escrita exige aprovação humana por operação.
- Messenger/DMs, `messages` webhook, resposta automática a comentário. — Fora do contrato de métricas; permissão e revisão adicionais.
- Instagram pessoal via API. — A API oficial não cobre; continua scraping.
- Download/armazenamento de mídia e de imagem de perfil. — Peso, direito autoral e LGPD sem uso.
- Backfill histórico além da janela pedida, ou recaptura diária de 30 dias de anúncio. — Captura incremental (dia anterior) na #12.
- Scheduler genérico/Celery/Kafka/RabbitMQ, container de worker, fila em Redis. — Fila é tabela no SQLite, consumida por um worker com lock de líder (ADR-0005).
- Prometheus/Grafana/OpenTelemetry. — `/v1/health` + log estruturado entregam o que a #11 pede; stack de observabilidade é outro projeto.
- Alertas por WhatsApp. — Issue separada, outro agente.
- Upgrade automático de versão da Graph API. — Versão é campo fixo; subir é decisão revisada, não cron.

**Plataforma**
- API v2, versionamento de contrato, deprecation policy. — `/v1` continua; nada de segundo prefixo agora.
- `create_all` em produção, "migração automática no lifespan". — Substituído pelo runner (§4).
- Criptografia do arquivo SQLite (SQLCipher, volume cifrado). — Fora de escopo; a chave estaria no mesmo host.
- Purge/retenção automática de `profile_snapshots`. — Sem requisito; 108 k linhas não são problema. Reavaliar aos 24 meses de dados, com o Leonardo.

## 6. Fechamentos que são meus (para colar nas issues)

| Issue | Aberto | Decisão |
|---|---|---|
| #7 | HTML/JS embutido ou framework | HTML estático + ES modules, sem build, sem CDN, assets em `src/api/static/admin/` |
| #7 | Tema escuro | Não |
| #7 | Quem opera | Um operador (Leonardo); sem multi-cliente |
| #4 | Um ou vários administradores | Um; tabela suporta N, UI não expõe |
| #4 | Biblioteca de hash | `argon2-cffi`, Argon2id 19 MiB/t=2/p=1 |
| #4 | Onde vive a senha | Hash no banco (`admin_users`); `ADMIN_PASSWORD_HASH` só no seed |
| #6 | Segredo no banco | Sim, cifrado, em tabela própria, com `APP_ENCRYPTION_KEY` só no ambiente |
| #6 | Histórico de overrides/rollback | Não; auditoria basta |
| #6 | Runner de migração | Próprio, SQL numerado; Alembic na primeira recriação de tabela |
| #5 | Recriar `HttpClient` | Não; proxy/timeouts frios (confirma ADR-0002) |
| #10 | Criptografia do token | AES-256-GCM, `APP_ENCRYPTION_KEY`, `key_version`, AAD por finalidade |
| #10 | Rotação de chave | Comando manual + `APP_ENCRYPTION_KEY_OLD`; sem automação |
| #10 | Multi-tenant | Não nesta entrega; sem `tenant_id` especulativo |
| #13 | Chave por cliente ou global | Chaves nomeadas, uma por integração |
| #13 | OAuth2/JWT | Não nesta fase |
| #13 | `/v1/system/status` | Autenticado com `read:metrics`; `/v1/health` público (healthcheck) |
| #13 | IP allowlist | Depois |
| #8 | `source` da Meta | `official_api` + `extra.provider="meta_graph"`; sem valor novo no enum |
| #8 | Janela de Insights | 28 dias, sobrescrevível por `since`/`until` |

Números que faltavam para o critério ficar testável:
- Chave de API: `avk_` + 32 bytes aleatórios em base64url (43 caracteres, 256 bits de entropia); hash SHA-256 hex com índice único; prefixo exibível = primeiros 12 caracteres.
- Por que SHA-256 e não Argon2 na chave: a chave é aleatória de 256 bits, não escolhida por humano; não há dicionário a proteger e o Argon2 custaria ~50 ms **por requisição**.
- Rate limit por chave: 120 req/min para `read:metrics`, 30 req/min para `write:scrape`, configurável; o limiter por plataforma continua sendo o gate final. Resposta 429 com `X-RateLimit-*` e `Retry-After`.
- Revogação: autenticação lê o banco por requisição (`SELECT` por `key_hash`, tabela pequena, WAL) — é o único jeito de "invalida na requisição seguinte" ser verdade. `last_used_at` atualizado no máximo 1×/min por chave.
- Sessão do painel: 12 h absolutas; 5 falhas de login por IP / 15 min.

## 7. Riscos residuais

1. **Segredo cifrado no banco** protege cópia do `.db` isolada, não comprometimento do host (onde estão `.env` e chave). Está declarado; não é falsa sensação de segurança.
2. **SQLite com um escritor**: painel, webhook e snapshots escrevem no mesmo arquivo. Mitigação já decidida (WAL, `busy_timeout=5000`, transações curtas, contadores fora do banco). Se `WEB_CONCURRENCY > 1` e o volume subir, o próximo passo é Postgres — não é esta entrega.
3. **Runner próprio** tem menos gente testando que Alembic. Mitigação: testes de paridade e idempotência; troca na primeira recriação de tabela.
4. **Hash no banco** é uma decisão que revoga o ADR-0004 do arquiteto. Se o Leonardo preferir "senha só no ambiente", o custo é: sem troca de senha pela tela e sem `must_change_password`.
5. **Chave de API em `/v1`** depende de o MetaNO confirmar que o webhook não aceita header customizado — premissa registrada, não fato verificado aqui.

## 8. Quando reavaliar

- Frontend: se aparecer requisito de gráfico interativo, múltiplos operadores ou tema — aí a conta do framework muda.
- Hash: se o login passar de ~1/s ou a VPS tiver < 512 MB livres, baixar `memory_cost` para 9 MiB (e regravar por `check_needs_rehash`).
- Criptografia: se o banco sair do host (Postgres gerenciado) ou aparecer requisito de KMS, trocar o provedor de chave mantendo o envelope (`nonce` + `key_version` + AAD).
- Schema: na primeira recriação de tabela, migrar para Alembic.
- Autenticação: se surgir cliente externo com necessidade de token de curta duração, aí OAuth2/JWT.
