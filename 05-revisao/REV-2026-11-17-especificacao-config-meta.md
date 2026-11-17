# Parecer de revisão — painel `/admin` e integração Meta

- **Revisor:** Revisor de Especificação · **Data:** 2026-11-17
- **Base revisada:** `main@d1c42dc` + branch `docs/plano-entrega@4ab5520`
- **Documentos revisados**
  - `docs/planejamento/00-plano.md` (branch `docs/plano-entrega`) — Líder de Planejamento
  - `Shared/analyticsoviet-requisitos/01-requisitos/RF-painel-config.md`, `RF-integracao-meta.md`, `decisoes-e-ordem.md` — Analista de Requisitos (**não versionados**)
  - `Shared/analyticsoviet-arquitetura/arquitetura-admin-config.md`, `arquitetura-meta.md`, `adr-config-e-meta.md` — Arquiteto de Software (**não versionados**)
  - Issues #1–#13 em `gunsleobezerra/analyticsoviet`
- **Não revisado, por ausência na data:** modelo de dados (Modelador) e fatos da Meta (MetaNO). Sem eles, #6/#10/#9/#13 e toda afirmação sobre a Meta seguem em aberto.

## Veredito

**Reprovado para implementação.** O desenho das duas frentes está consistente com o código e resolve os dois furos reais (`enable_history` e `--workers`), mas: os documentos de requisitos e arquitetura não estão em git; os critérios de aceite apontam para 21 critérios Gherkin que não existem; e três conflitos entre plano, requisitos e arquitetura mudam tabela, permissão e contrato. Nenhum deles é resolvível pelo implementador.

Pronto: leitura do código, pontos de integração arquivo por arquivo, classificação quente/fria, fallback Graph→scraping, fluxo OAuth com `state` de uso único, fila com lock de líder.

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

### B6. Troca obrigatória de senha sem lugar para guardar o hash
UC-CFG-01 (4c) e o plano exigem troca da senha inicial no primeiro acesso. ADR-0004 e a arquitetura gravam `ADMIN_PASSWORD_HASH` só no ambiente e dizem que trocar a senha é trocar a variável — não há onde persistir o hash novo.
**Pergunta:** tabela de credencial no banco (com hash) ou a troca obrigatória sai do escopo?

### B7. Auditoria: uma linha por escrita ou por campo?
Issue #4: "Toda escrita gera exatamente uma linha de auditoria". RF-CFG-08: "uma linha por campo alterado". Arquitetura: "uma linha por transação". Um `PATCH` de 3 campos gera 1 ou 3 linhas? O critério de aceite não é escrevível sem isso.

### B8. Invalidação de cache por TTL: decisão inimplementável
O plano manda "limpar as chaves da plataforma afetada". `src/core/cache.py` expõe só `get/set/delete/close/stats` — não há listagem nem padrão — e o Redis não tem `SCAN` seguro (a própria arquitetura descartou essa via). Ou vale o epoch por família de TTL da arquitetura, ou se aceita que o TTL novo só vale para chave nova.
**Impacto:** o critério "muda na requisição seguinte" (#5, CA-CFG-12) é falso para entrada já gravada. Sem a decisão, o teste não passa.

### B9. Janela de Insights: 28 ou 30 dias?
Issue #8 aberta em 7/28/30; analista 28; arquiteto 28; plano 30. Um número, com o motivo. Define default de campo e critério.

### B10. Campos de webhook: três escopos diferentes
Analista: só `feed` da Página. Arquiteto: `feed`, `comments`, `mentions`; `messages` fora. Plano: Página com `feed`/`comments`/`mentions` **e** Instagram com `comments`/`mentions`. Muda tabela, permissões e App Review.
**Pergunta:** qual lista vale na primeira entrega?

### B11. `source` da Graph não existe no enum
Issue #8 pede `source: "meta_graph"`. `Source` (`src/models/schemas.py:25`) tem `html`, `embedded_json`, `official_api`, `oembed`, `ytdlp`. A arquitetura decidiu `official_api` + `extra.provider`. O corpo da issue precisa de comentário de correção, senão o implementador cria valor novo e quebra quem compara `source`.

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
| #4 | auditoria | uma linha por campo alterado, na mesma transação |
| #4 | senha | tabela de credencial com hash, ou remover troca obrigatória |
| #5 | invalidação de TTL | epoch por família (arquitetura); descartar "limpar chaves" |
| #5 | `public_base_url` | frio |
| #6 | assinaturas | critério reescrito como contrato público inalterado |
| #8 | janela | 28 dias, sobrescrevível (analista e arquiteto) |
| #8 | `source` | `official_api` + `extra.provider = "meta_graph"` |
| #9 | campos de webhook | `feed` da Página apenas, na primeira entrega |
| #10 | chave de criptografia | `read_only`, fora do schema do painel |

## Ordem revisada

Configuração: **#6 → #4 → #3 → #13 → #5 → #7**.
A #13 depois de #3 porque a gestão de chaves é tela do painel; antes de #10/#9 porque a camada de autenticação precisa existir para que as isenções de callback/webhook sejam declaradas, não improvisadas.

Meta: **MetaNO (dia 0) → #10 → #11 → #8 → #12 → #9**, com #7 em paralelo a #10. Sem os fatos do MetaNO, #10 não tem escopo de permissão e #9 não tem requisito de verificação.

A ordem do plano está correta exceto pela ausência da #13 e pela posição de #7/#5, que o analista inverte em relação ao líder sem justificar. Uma das duas vale.

## O que não verifiquei

- Fatos da Meta (versão da Graph, permissões, tokens, quota, webhook, App Review, Business Discovery): MetaNO não publicou. Toda afirmação sobre a Meta nestes documentos é premissa, não fato.
- Modelo de dados: não publicado. Sem ele não dá para revisar PK/FK, índices, migração nem o que a #13 e a #10 exigem.
- Decisões do Tech Lead: não publicadas (frontend, hash, criptografia, versionamento de schema).
- Se o Coolify termina TLS e se existe app Meta: depende do Leonardo.
