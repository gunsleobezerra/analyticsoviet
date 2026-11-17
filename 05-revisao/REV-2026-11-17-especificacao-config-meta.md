# Parecer de revisão — painel `/admin` e integração Meta

- **Revisor:** Revisor de Especificação · **Data:** 2026-11-17
- **Base revisada:** `main@d1c42dc` + branch `docs/plano-entrega@4ab5520`
- **Documentos revisados**
  - `docs/planejamento/00-plano.md` (branch `docs/plano-entrega`) — Líder de Planejamento
  - `Shared/analyticsoviet-requisitos/01-requisitos/RF-painel-config.md`, `RF-integracao-meta.md`, `decisoes-e-ordem.md` — Analista de Requisitos (**não versionados**)
  - `Shared/analyticsoviet-arquitetura/arquitetura-admin-config.md`, `arquitetura-meta.md`, `adr-config-e-meta.md` — Arquiteto de Software (**não versionados**)
  - `04-dados/` na branch `dados/modelo-config-meta@12c6d86` — Modelador de Dados
  - Issues #1–#13 em `gunsleobezerra/analyticsoviet`
- **Não revisado, por ausência na data:** fatos da Meta (MetaNO) e decisões técnicas (Tech Lead). Sem eles, #10/#11/#9 e toda afirmação sobre a Meta seguem em aberto.

## Veredito

**Reprovado para implementação.** O desenho das duas frentes está consistente com o código e resolve os dois furos reais (`enable_history` e `--workers`), mas: os documentos de requisitos e arquitetura não estão em git; os critérios de aceite apontam para 21 critérios Gherkin que não existem; três conflitos entre plano, requisitos e arquitetura mudam tabela, permissão e contrato; e a migração 002 falha como está escrita. Nenhum deles é resolvível pelo implementador.

Pronto: leitura do código, pontos de integração arquivo por arquivo, classificação quente/fria, fallback Graph→scraping, fluxo OAuth com `state` de uso único, fila com lock de líder, modelo lógico com PK/FK/UNIQUE em todas as tabelas, dinheiro em inteiro com expoente da moeda e rollback da migração.

## Bloqueia

### B1. Requisitos e arquitetura não estão em git
Os dois documentos existem só em `Shared/`. `git ls-remote origin` mostra apenas `main` e `docs/plano-entrega`; `docs/especificacao/` — caminho citado no próprio plano — não existe no repositório. O repo de artefatos (`Shared/repos/analyticsoviet.git`) também não foi criado.
**Pergunta:** quem publica `01-requisitos/` e `03-arquitetura/` em branch, e o plano passa a apontar para o repo de artefatos em vez de `docs/especificacao/`?

### B2. A issue #13 não existe no plano
O plano declara no fora de escopo: "Autenticação da API pública `/v1/*` — continua aberta como hoje". A #13 nasceu depois e não aparece em dono, ordem, dependência nem decisões. Nenhum documento trata tabela `api_keys`, migração, painel de chaves ou rate limit por chave.
**Pergunta:** o plano v2 inclui a #13 na trilha A? Onde — depois de #3 (gestão de chaves mora no painel) e antes de #10?

### B3. Chave de API em `/v1` quebra OAuth e webhook
A #13 manda aplicar a dependência de chave no router `/v1` inteiro. `GET /v1/meta/oauth/callback` (#10) chega pelo navegador e `POST /v1/meta/webhook` (#9) chega pela Meta; nenhum dos dois envia chave. Resultado: 401 e as duas frentes param.
**Pergunta:** qual a lista fechada de rotas isentas, e onde ela fica escrita?

### B4. Chave de API em `/v1/health` derruba o container
`docker-compose.yml` (healthcheck) e `Dockerfile` (`HEALTHCHECK`) chamam `/v1/health` sem chave a cada 30 s. A #13 deixa "`system.py` pode ficar público — decidir". Se ficar autenticado, o container entra em `unhealthy` e o Coolify reinicia em loop.
**Pergunta:** `/v1/health` público e `/v1/system/status` autenticado — confirma?

### B5. `/mcp` fica fora de qualquer autenticação
`src/main.py` monta `mcp.sse_app` em `/mcp` com `allowed_origins=["*"]`; `src/mcp_server.py` monta o próprio `AppState` via `get_settings()` e chama `_dispatch_url` direto, sem passar por `/v1`. A chave em `/v1` não cobre esse caminho: o scraping continua aberto por outra porta, e a configuração quente nunca chega ao MCP (ele lê `get_settings()` no primeiro uso e congela).
**Pergunta:** `/mcp` entra no escopo da #13 e do #5, ou fica como risco aceito por escrito? Hoje não está em nenhum dos dois.

### B6. Troca obrigatória de senha: três documentos, três desenhos
UC-CFG-01 (4c) e o plano exigem troca da senha inicial no primeiro acesso; ADR-0004 grava `ADMIN_PASSWORD_HASH` só no ambiente e diz que trocar a senha é trocar a variável; o modelo criou `admin_users` com `password_hash`, `must_change_password` e `password_changed_at`.
**Pergunta:** vale o modelo (`admin_users` no banco, com seed do ambiente)? Então o ADR-0004 precisa ser reescrito, senão o implementador segue o ADR e a troca de senha não existe.

### B7. Auditoria: uma linha por escrita ou por campo?
Issue #4: "Toda escrita gera exatamente uma linha de auditoria". RF-CFG-08: "uma linha por campo alterado". Modelo: uma linha por chave, agrupada por `request_id`. Um `PATCH` de 3 campos gera 1 ou 3 linhas?
**Pergunta:** confirma uma linha por campo com `request_id` (modelo + RF) e corrige o corpo da #4?

### B8. Invalidação de cache por TTL: decisão inimplementável
O plano manda "limpar as chaves da plataforma afetada". `src/core/cache.py` expõe só `get/set/delete/close/stats` — não há listagem nem padrão — e o Redis não tem `SCAN` seguro (a própria arquitetura descartou essa via). Ou vale o epoch por família de TTL da arquitetura, ou se aceita que o TTL novo só vale para chave nova.
**Impacto:** o critério "muda na requisição seguinte" (#5, CA-CFG-12) é falso para entrada já gravada. Sem a decisão, o teste não passa.

### B9. Janela de Insights: 28 ou 30 dias?
Issue #8 aberta em 7/28/30; analista 28; arquiteto 28; plano 30. Um número, com o motivo. Define default de campo e critério.

### B10. Campos de webhook: três escopos diferentes
Analista: só `feed` da Página. Arquiteto: `feed`, `comments`, `mentions`; `messages` fora. Plano: Página com `feed`/`comments`/`mentions` **e** Instagram com `comments`/`mentions`. Muda tabela, permissões e App Review.
**Pergunta:** qual lista vale na primeira entrega?

### B11. `source` da Graph não existe no enum
Issue #8 pede `source: "meta_graph"`. `Source` (`src/models/schemas.py:25`) tem `html`, `embedded_json`, `official_api`, `oembed`, `ytdlp`. A arquitetura decidiu `official_api` + `extra.provider`; o modelo diz que `profile_snapshots.source` "passa a receber `meta_graph`/`meta_webhook` — valor novo de enum no contrato". Duas decisões opostas.
**Pergunta:** `official_api` + `extra.provider` (arquitetura) ou valor novo no enum (modelo)? O corpo da #8 precisa de comentário de correção em qualquer caso, senão o implementador escolhe sozinho.

### B12. `META_TOKEN_ENCRYPTION_KEY` editável no painel apaga os tokens
É a chave que decifra os tokens em repouso. Se entrar no schema como campo sensível comum (cifrado com `CONFIG_SECRET_KEY`), trocá-la pela tela torna todos os tokens ilegíveis. Nenhum documento a marca como `read_only`/fora do painel.

### B13. `public_base_url` é frio, não quente
O plano o lista como quente. Ele é lido em `create_app` (`build_servers`) e no Swagger: mudar em runtime não altera nada. A arquitetura já o classifica como frio — corrigir o plano, senão o #5 ganha um critério impossível.

### B14. Convergência entre workers sem número nem forma de teste
RNF-CFG-06 exige "a mesma configuração em todos os workers", sem número, e a suíte roda em um processo (`pytest` + TestClient) com `WEB_CONCURRENCY=1` no compose. Não há teste possível como escrito.
**Pergunta:** aceita-se convergência ≤ 1 s com teste de integração que sobe 2 workers (fora do CI padrão), ou o escopo fica em 1 worker nesta entrega?

### B15. Critérios de aceite que não existem
As tabelas de RF apontam para `CA-CFG-01..17` e `CA-META-01..22`, mas só existem 7 critérios CFG (01, 03, 06, 07, 09, 12, 13) e 11 META (01, 02, 04, 06, 11, 12, 13, 15, 16, 19, 21). **21 RFs sem critério escrito**, incluindo todos os de segurança de sessão, CSRF, idempotência de webhook e quota. Requisito sem critério testável não passa.

### B16. Diagramas C4 não renderizam
`C4Context`, `C4Container` e `C4Component` não são tipos suportados pelo Mermaid. No GitHub, os seis diagramas aparecem como erro. Trocar por `flowchart` com subgrafo (um por container) ou manter a tabela C4 em texto e usar Mermaid só para sequência/estado.

### B17. A migração 002 falha com `foreign_keys=ON`
`04-dados/ddl/002-config-admin.sql` cria `config_state` com FK para `admin_users` e **insere a linha `id=1` antes de criar `admin_users`**. Com o `PRAGMA foreign_keys=ON` que o próprio plano de migração exige, o `INSERT` falha com `no such table: main.admin_users` e a etapa 2 aborta.
**Correção:** criar `admin_users` antes de `config_state`, ou inserir a linha depois. Enquanto isso, a migração não roda.

### B18. Formato de data/hora não está fixado
As tabelas novas usam `TEXT` "UTC", mas o DDL semeia com `strftime('%Y-%m-%d %H:%M:%f','now')` (espaço, milissegundos) e o código Python escreveria `datetime.isoformat()` (`T`, microssegundos, `+00:00`). A tabela existente `profile_snapshots` grava pelo SQLAlchemy (`DateTime`, `YYYY-MM-DD HH:MM:SS.ffffff`). Misturar formatos quebra `CHECK (expires_at > created_at)`, `ORDER BY changed_at` e consulta por faixa — sem erro, só resultado errado.
**Pergunta:** qual formato canônico, escrito no modelo? (proposta: `YYYY-MM-DD HH:MM:SS.ffffff` UTC, sem offset, igual ao SQLAlchemy atual)

### B19. Segredo no banco: modelo recusa, #6 decidiu o contrário
`04-dados/README.md` decisão 1: "nenhum segredo em tabela de configuração; `config_overrides` recusa campo sensível (422)". A decisão fechada de #6 (analista e ADR-0006) é o oposto: segredo cifrado no banco com chave do ambiente. O modelo deixa como "aberto — Tech Lead".
**Impacto:** sem coluna de segredo, `META_APP_SECRET` e `S3_SECRET_ACCESS_KEY` não podem ser cadastrados pelo painel, e metade do objetivo da frente Config cai. **Pergunta:** qual das duas vale? Se for o modelo, o escopo do painel precisa dizer que segredo continua no `.env`.

## Corrige depois

1. **"5 TTLs de cache"** nos três documentos. `Settings` tem quatro: `profile`, `posts`, `post`, `blocked` (`src/core/config.py:57-61`).
2. **#6 aceite "nenhuma chamada existente muda de assinatura além de `get_settings()`"** é falso: `build_state`, `AdapterContext` e `PlatformLimiter` mudam — a arquitetura manda mudar. Reescrever como "nenhuma rota pública muda de contrato".
3. **#3 aceite "documentação do painel em `/docs`"** contradiz a correção do arquiteto (`include_in_schema=false`) e expõe rota administrativa no contrato público. Escolher: OpenAPI separado em `/admin/openapi.json`.
4. **#7 "nenhum recurso externo"**: `install_docs` cai para CDN quando o bundle não está instalado (`src/api/docs.py:263`). O critério só é verificável com o bundle presente; o teste precisa varrer o HTML servido por URL externa.
5. **`forbidden` não existe** em `src/core/errors.py`. A arquitetura deixa "ou `forbidden` se o Tech Lead criar". Fechar: `auth_required` com 403 ou nova classe (muda contrato).
6. **Custo do `/v1/health`**: o bloco `meta` consulta o banco a cada 30 s pelo healthcheck. Exigir "sem rede e com cache curto" e teste de que health não chama a Graph.
7. **`HealthResponse` é `response_model` fechado**: campo `meta`/`config` precisa de default e o exemplo em `model_config` precisa ser atualizado, senão `/docs` mente.
8. **Fallback por latência** ("Graph ≤ 1,2 s p95 senão fallback", arquitetura-meta riscos #9) não é requisito testável e não deve existir: fallback por erro/timeout, não por lentidão.
9. **`instagram_app_id`** (`src/core/config.py:90`, default fixo, usado como `x-ig-app-id` em `src/platforms/instagram.py:285`) não é o `META_APP_ID` da #10. Dizer isso na issue, senão alguém reaproveita o campo.
10. **Nomes divergentes**: `ADMIN_PASSWORD` × `ADMIN_PASSWORD_HASH`, `META_ENCRYPTION_KEY` × `META_TOKEN_ENCRYPTION_KEY`, `CONFIG_SECRET_KEY` × `config_secret_key`. Padronizar antes de implementar.
11. **Retenção e exclusão**: snapshots ao desconectar, eventos de webhook, auditoria — sem issue e sem número. Nenhum documento cita base legal/LGPD nem o callback de exclusão de dados que a Meta exige de app com dados de usuário (**a confirmar com MetaNO**).
12. **RNF-META-06** ("TTL próprio") sem número; **#11 "chamadas na janela"** sem definir a janela; **#13 rate limit por chave** sem requisições/janela e sem entropia mínima da chave.
13. **Critérios com "ou"**: #5 ("usa o novo proxy **ou** o campo é frio"), #12 ("completos **ou** com indicação de truncamento"). Critério com alternativa não é critério; escolher um ramo.
14. **FK sem índice** no modelo: `config_audit_log.actor_user_id`, `meta_usage_hourly.asset_id` (140 mil linhas/ano), `meta_tokens.asset_id` e `api_keys.created_by`. Com `foreign_keys=ON`, apagar o pai varre a tabela filha. `meta_usage_hourly` precisa de índice explícito em `asset_id` — o índice por expressão em `COALESCE(asset_id,'')` não atende `WHERE asset_id = ?`, como o próprio documento avisa.
15. **`config_overrides.key` em MAIÚSCULAS** (`CACHE_TTL_PROFILE_SECONDS`) enquanto o contrato administrativo usa o nome do campo em minúsculas (`database_url`, CA-CFG-03). A regra de conversão precisa estar escrita no contrato, senão o painel manda `database_url` e o banco grava `DATABASE_URL`.
16. **`graph_version` em dois lugares**: `meta_connections.graph_version` (modelo) e `meta_graph_version` em `Settings` (arquitetura). Dizer qual é a fonte de verdade e o que acontece quando divergem.
17. **`config_state.updated_by` com `ON DELETE SET NULL`** e `config_overrides.updated_by` com `RESTRICT` na mesma entidade: desativar usuário funciona, apagar não. É coerente com "usuário nunca é apagado", mas o DDL permite os dois caminhos; alinhar.

## Modelo de dados — parecer

Checado item a item: PK em todas as 18 tabelas; FK declarada com `ON DELETE` explícito; cardinalidade nas duas pontas no ER; dinheiro em inteiro com `currency_exponent` (resolve KWD/JPY sem `REAL`); período em `YYYY-MM-DD` no fuso da conta e captura em UTC; migração com rollback documentado por etapa e baseline para banco já existente; volume com premissa explícita.

Falhas: B17 (a 002 não roda com FK ligada), B18 (formato de data não fixado), B19 (segredo recusado contra a decisão de #6) e os índices de FK do item 14. O resto é sólido — é o documento com melhor rastreabilidade da entrega.

## Sugestões

- Teste de meta-cobertura: iterar `Settings.model_fields` e falhar se algum campo não tiver classificação e entrada no schema. É o que torna real o critério "campo novo aparece no painel" (#7).
- Usar `config_version` como `ETag`/`If-Match` no `PATCH`: duas abas abertas hoje sobrescrevem uma à outra sem aviso.
- `GET /admin/api/config?reveal=non_secret` para diagnóstico pós-incidente, sem segredo.
- Job de CI separado com 2 workers uvicorn para o RNF-CFG-06, marcado como integração.

## Decisões que precisam ser fechadas (conflito → proposta)

| Issue | Conflito | Proposta de fechamento |
|---|---|---|
| #13 | fora de escopo no plano | incluir na trilha A, entre #3 e #7; bloqueia #10/#9 |
| #13 | rotas isentas | isentar `oauth/callback`, `webhook`, `health`; `system/status` autenticado |
| #13 | `/mcp` | incluir na #13 (mesma dependência) ou risco aceito por escrito |
| #4 | auditoria | uma linha por campo, agrupada por `request_id` (modelo + RF) |
| #4 | senha | vale `admin_users` do modelo; reescrever ADR-0004 |
| #5 | invalidação de TTL | epoch por família (arquitetura); descartar "limpar chaves" |
| #5 | `public_base_url` | frio |
| #6 | assinaturas | critério reescrito como contrato público inalterado |
| #6 | segredo no banco | decidir: cifrado no banco (analista/ADR) ou só ambiente (modelo) |
| #8 | janela | 28 dias, sobrescrevível (analista e arquiteto) |
| #8 | `source` | `official_api` + `extra.provider = "meta_graph"` (arquitetura) |
| #9 | campos de webhook | `feed` da Página apenas, na primeira entrega |
| #10 | chave de criptografia | `read_only`, fora do schema do painel |

## Ordem revisada

Configuração: **#6 → #4 → #3 → #13 → #5 → #7**.
A #13 depois de #3 porque a gestão de chaves é tela do painel; antes de #10/#9 porque a camada de autenticação precisa existir para que as isenções de callback/webhook sejam declaradas, não improvisadas.

Meta: **MetaNO (dia 0) → #10 → #11 → #8 → #12 → #9**, com #7 em paralelo a #10. Sem os fatos do MetaNO, #10 não tem escopo de permissão e #9 não tem requisito de verificação.

A ordem do plano está correta exceto pela ausência da #13 e pela posição de #7/#5, que o analista inverte em relação ao líder sem justificar. Uma das duas vale.

## O que não verifiquei

- Fatos da Meta (versão da Graph, permissões, tokens, quota, webhook, App Review, Business Discovery): MetaNO não publicou. Toda afirmação sobre a Meta nestes documentos é premissa, não fato.
- Decisões do Tech Lead: não publicadas (frontend, hash, criptografia, versionamento de schema). O modelo recomenda runner próprio em vez de Alembic — decisão ainda pendente.
- Se o Coolify termina TLS e se existe app Meta: depende do Leonardo.
- O DDL foi lido, não executado. A falha do B17 é de ordem de DDL e de semântica do `PRAGMA foreign_keys`, não de sintaxe.

---

# Rodada 2 — 2026-11-17, após Tech Lead e MetaNO

Novas fontes: `Shared/analyticsoviet-techlead/decisoes-tecnicas.md` (Tech Lead) e `Shared/analyticsoviet-meta/00-fatos-meta.md` (MetaNO), mais os comentários publicados nas issues #1, #4, #5, #6, #7, #8, #9, #10, #11, #12 e #13.

## Bloqueios desta rodada que foram fechados

| # | Item | Fechado por |
|---|---|---|
| B3 | Rotas isentas de chave | Tech Lead: `/v1/health`, callback OAuth, webhook |
| B4 | `/v1/health` público, `/v1/system/status` autenticado | Tech Lead + MetaNO |
| B5 | `/mcp` aberto | Decisão: `MCP_ENABLED=false` por padrão e chave quando ligado — **implementação ainda não fecha, ver R2-6** |
| B6 | Três desenhos de senha | Tech Lead: `admin_users` + Argon2id; hash no banco, ambiente só como seed |
| B9 | Janela de Insights | 28 dias, sobrescrevível |
| B11 | `source` da Graph | `official_api` + `extra.provider="meta_graph"`, sem valor novo no enum |
| B12 | Chave de criptografia editável | `APP_ENCRYPTION_KEY` somente ambiente |
| B19 | Segredo no banco | Fechado a favor do arquiteto: cifrado, em `config_secrets`, com `key_version` |

Consequência imediata para dois artefatos: o **modelo** precisa criar `config_secrets` e aceitar segredo cifrado (hoje recusa com 422), e o **ADR-0004** do arquiteto precisa ser reescrito (diz que hash de senha vive só no ambiente). Sem esses dois ajustes, os bloqueios fechados por decisão continuam abertos no documento.

## Bloqueios que continuam abertos

- **B1** Requisitos e arquitetura fora do git; repo de artefatos inexistente.
- **B2** #13 fora do plano; o plano v1 ainda diz "autenticação de `/v1` fora de escopo".
- **B7** Auditoria: uma linha por escrita (#4) × por campo (RF e modelo). Acordo existe no modelo, corpo da issue não mudou.
- **B8** Invalidação de cache: o plano ainda manda "limpar chaves da plataforma", o que o `Cache` não permite.
- **B10** Campos de webhook: MetaNO não confirmou a lista literal dos campos da Página (declarou como não verificado), então a decisão "só `feed`" segue como premissa, não fato.
- **B15** 21 RFs sem critério Gherkin escrito.
- **B16** Diagramas C4 não renderizam em Mermaid.
- **B17** Migração 002 falha: `config_state` referencia `admin_users` e insere linha antes de a tabela existir.
- **B18** Formato de data não fixado (`strftime` × `isoformat` × `DateTime`).

## Novos bloqueios — revisão da #13 (pedido do dono)

**R2-1. Não existe tabela rota → escopo, e sem ela os escopos não separam nada.**
`/v1/profile/{platform}/{handle}`, `/v1/post`, `/v1/metrics/{platform}/{handle}` e `POST /v1/scrape` vão todos ao upstream. Se `read:metrics` cobrir `/v1/metrics`, uma chave de leitura dispara scraping, contra o objetivo declarado da #13. O único leitor puro é `GET /v1/history`. Sem a classificação por rota, o critério "chave sem escopo → 403" não é escrevível.

**R2-2. Escopo `admin` não autoriza nenhuma rota.** `/admin/api/*` é sessão de painel e a própria #13 proíbe chave lá. O valor existe só no `CHECK` de `api_key_scopes`. Escopo morto que aparenta poder.

**R2-3. "Comparação em tempo constante" é critério sem objeto.** A autenticação busca por `key_hash` em índice único; não há comparação em aplicação para ser constante. Manter o texto convida a implementar algo inútil e a omitir o que importa: hash fora de log e de auditoria.

**R2-4. `API_AUTH_ENABLED` tem três classificações simultâneas** — frio (#6), quente com auditoria (Arquiteto), `env_only` (Analista). A #7 gera o formulário do schema, então isso vira três telas. Com precedência banco > ambiente, `false` gravado no banco abre a API pública no próximo restart.

**R2-5. A API pública passa a depender do SQLite para autenticar.** Ler a chave por requisição é o único jeito de "revogada invalida na requisição seguinte" ser verdade, mas transforma indisponibilidade do banco em 503 para todo `/v1`. É decisão de disponibilidade, precisa estar escrita (fail closed, `/v1/health` público, teste com banco fechado).

**R2-6. `/mcp` não aceita `Depends`.** `src/main.py` usa `app.mount("/mcp", mcp.sse_app(...))`; dependência FastAPI não se aplica a `mount`. "Exige a mesma chave quando ligado" só vale com middleware ASGI na frente do mount — hoje o critério não tem onde ser implementado.

## Novos achados — corrige depois

1. Unidade do limite por chave: 30/min por requisição ou por URL? Lote de `scrape_max_urls=20` multiplica por 20 e estoura `ratelimit_instagram_per_minute=20`. MetaNO confirma que N IDs = N chamadas.
2. Onde o contador vive: modelo diz Redis/cache, Tech Lead não decidiu. Com `CACHE_BACKEND=memory` e 3 workers, o limite efetivo é 3×.
3. Tabela de erro da autenticação ausente: 401 com `WWW-Authenticate`, 403 para escopo, 429 com `Retry-After`, e `Authorization` × `X-API-Key` divergentes.
4. `Cache-Control: no-store` na resposta que devolve a chave em claro.
5. Rotação × revogação: "antiga válida por 24 h" e "revogada invalida na requisição seguinte" exigem rotação = linha nova + `expires_at`, não `revoked_at`.
6. Prefixo: modelo aceita 12–16 caracteres; Tech Lead define 12 primeiros. Fixar um.
7. `/v1/system/status` é rota nova: não existe em `src/api/routes/system.py` (só `/health` e `/platforms`). Precisa de contrato e de modelo de resposta; `HealthResponse` hoje é fechado e não tem bloco `meta`.

## Veredito da rodada 2

**Continua reprovado para implementação**, por menos motivos: os bloqueios de decisão técnica e de fato da Meta foram fechados. O que trava agora é de documentação e de contrato — modelo e ADR desatualizados em relação às decisões, plano v1 sem a #13, migração 002 que não roda, e a #13 sem tabela rota→escopo. Nenhum exige nova decisão de terceiro; todos exigem consolidação.

## Ainda não verifiquei

- Lista literal de campos de webhook da Página e condições de invalidação do token de Página: o próprio MetaNO marcou como não confirmado.
- DDL não executado; a falha da 002 é de ordem de DDL, não de sintaxe.
- Se o Coolify termina TLS e se o app Meta existe: depende do Leonardo.
