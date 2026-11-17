# Modelo de dados — painel de configuração + integração Meta

Branch: `dados/modelo-config-meta`. Autora: Modelador de Dados. Base: `main` em `d1c42dc`.
Escopo: apenas estrutura de dados. Nada aqui foi executado em banco — `ddl/` é referência para outra pessoa rodar.

## Estado atual (verificado no código)

| Fato | Onde | Consequência para o modelo |
|---|---|---|
| Um único banco SQLite: `sqlite+aiosqlite:///./data/analyticsoviet.db` | `src/core/config.py`, `docker-compose.yml` | todo o modelo novo é SQLite; sem `jsonb`, sem `uuid`, sem `timestamptz` |
| Tabelas criadas por `Base.metadata.create_all` no boot | `src/db/session.py` | `create_all` **não altera** tabela existente: a coluna nova em `profile_snapshots` exige migração versionada |
| Não há Alembic nem tabela de versão | `pyproject.toml`, `src/db/` | é preciso introduzir controle de versão de schema |
| `profile_snapshots` é a única tabela; grava a cada leitura de perfil | `src/db/models.py`, `src/db/snapshots.py` | crescimento proporcional ao uso, não ao número de contas |
| `source` e `confidence` já existem em `profile_snapshots` | `src/db/models.py` | origem oficial/`meta_graph` cabe sem coluna nova |
| Timeout/retry/rate limit por plataforma existem só em memória | `src/core/ratelimit.py`, `src/core/http.py` | contadores de quota não devem ir para o SQLite a cada chamada |
| `WEB_CONCURRENCY` pode ser > 1 | `Dockerfile` | vários processos escrevendo no mesmo arquivo SQLite: WAL + escrita curta obrigatórios |

## Entregas

- [`er/modelo-conceitual.md`](er/modelo-conceitual.md) — ER com cardinalidade nas duas pontas.
- [`modelo-logico.md`](modelo-logico.md) — tabelas, PK/FK/UNIQUE/nulidade e normalização justificada.
- [`indices-e-volume.md`](indices-e-volume.md) — cada índice com a consulta que atende; volume e crescimento.
- [`ddl/`](ddl/) — DDL de referência (SQLite), em ordem de aplicação.
- [`migracao.md`](migracao.md) — passos aditivos, rollback, janela, backfill e pragmas.

## Decisões fechadas por mim (com motivo)

1. **Nenhum segredo em tabela de configuração nesta entrega.** `config_overrides` recusa campo sensível (422). Motivo: chave de criptografia no ambiente + cópia do `.db` circulando = segredo exposto; e o `.env` já resolve bootstrap. Consequência: `META_APP_SECRET`, `S3_SECRET_ACCESS_KEY` e afins continuam só em ambiente. Reavaliar quando existir cofre com rotação.
2. **O tipo do campo não é coluna do banco.** `config_overrides` guarda só `key` + `value_json`. O tipo, a faixa válida e o "exige reinício" vêm de `Settings` (código). Motivo: duplicar tipo no banco cria duas verdades que divergem no primeiro rename; 3FN.
3. **Auditoria é append-only e uma linha por chave alterada**, agrupadas por `request_id`. Motivo: "exatamente uma linha por escrita" da #4 fica verificável e um reset de seção continua rastreável campo a campo.
4. **Contadores de quota e de rate limit por chave não vão para o SQLite.** Ficam no cache (Redis em produção). Motivo: uma escrita por chamada HTTP no SQLite serializa todo o serviço. No banco fica só o agregado horário `meta_usage_hourly`, que serve `/v1/health` e histórico.
5. **Insights em formato longo (`métrica` + `valor`), não 20 colunas nullable.** Motivo: o conjunto de métricas da Meta muda por versão da API; coluna nova em SQLite exige migração, linha nova não. Ausência de métrica = linha ausente (preserva "`null`, nunca zero").
6. **`meta_assets` único por `(asset_type, external_id)`**, cobrindo Página, Instagram profissional e conta de anúncios. Motivo: são o mesmo tipo de coisa (objeto da Meta que recebe token e produz métrica); tabelas separadas triplicariam token, status e vínculo.
7. **Histórico não perde a origem.** Snapshot oficial, de webhook e de scraping convivem na mesma tabela com `source` + `inserted_by`; leitura de série filtra por `source` quando a comparação precisa ser homogênea.
8. **Exclusão é estado, não `DELETE`.** Conta desconectada vira `status='disconnected'`; snapshots antigos continuam válidos. `ON DELETE` só onde a parte não existe sem o todo (token, escopo, payload de webhook).

## Aberto — precisa de decisão de outro papel

| Pergunta | Quem decide | O que muda no modelo |
|---|---|---|
| Um usuário administrador ou vários? | Tech Lead / Leonardo (#4) | modelo já suporta N; UI pode restringir |
| Guardar segredo no banco (cifrado) ou só no ambiente? | Tech Lead (#6) | se sim, entram `secret_ciphertext`/`key_version` em `config_overrides` |
| Multi-cliente (multi-tenant) desde já? | Leonardo (#10) | se sim, entra `tenant_id` em `meta_connections`/`meta_assets` **antes** da primeira conta real |
| Guardar payload cru de webhook por quanto tempo? | Leonardo (#9) + MetaNO | hoje: só em falha, 7 dias |
| Breakdown de anúncio (idade, gênero, posicionamento) nesta entrega? | Leonardo (#12) | `breakdown_key` já reservado na unicidade, sem migração depois |
| Retenção de `profile_snapshots` (hoje infinita) | Leonardo | partição/purga; SQLite só DELETE em lote |
| Versão da Graph API e permissões | MetaNO | `graph_version` em `meta_connections`, sem impacto estrutural |

## Volume assumido

Premissa explícita (a confirmar com o Leonardo): **até 10 perfis acompanhados, 3 contas Meta, ≤ 300 leituras de perfil/dia, ≤ 5 000 eventos de webhook/dia, 1 conta de anúncios**. Detalhe e crescimento por tabela em [`indices-e-volume.md`](indices-e-volume.md).
