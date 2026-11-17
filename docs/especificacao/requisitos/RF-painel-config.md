# Especificação de requisitos — Painel gráfico de configuração (`/admin`)

- **Épico:** #2 · **Issues:** #4, #5, #6, #3, #7
- **Repo de código:** `gunsleobezerra/analyticsoviet` (clone de leitura em `Shared/analyticsoviet`)
- **Autor:** Analista de Requisitos · **Data:** 2026-11-17
- **Estado:** para revisão. Toda decisão de biblioteca/algoritmo é do Tech Lead; aqui só o comportamento exigido.

## 1. Problema e objetivo

Hoje os ~50 parâmetros de `src/core/config.py` (`Settings`, pydantic-settings) só existem como variável de ambiente em `.env`, lidos uma vez por `get_settings()` (`@lru_cache`) e capturados no `lifespan` de `src/main.py`, que monta `HttpClient`, `Cache`, `Database` e `AdapterRegistry` uma única vez (`src/api/deps.py`). Consequência: qualquer ajuste exige editar arquivo na VPS e reiniciar o container.

**Objetivo:** um operador autenticado vê o estado ativo de cada parâmetro, altera com validação, testa conexão de provedores e sabe exatamente o que exige reinício — sem editar arquivo e sem expor segredo.

**Fora de escopo:** multi-tenant de operadores, tema escuro, edição de `.env` pelo painel, rollback versionado de configuração, criação de campo novo pelo painel.

## 2. Atores

| Ator | Descrição |
|---|---|
| **Operador** | Administrador único do serviço. Autentica no painel, lê e altera configuração, testa conexões. |
| **Consumidor da API pública** | Cliente de `/v1/*`. Não autentica e não percebe o painel, exceto por mudanças de comportamento. |
| **Sistema (boot)** | `lifespan` de `src/main.py`. Resolve a configuração efetiva e monta os componentes. |

## 3. Casos de uso

### UC-CFG-01 — Autenticar no painel

- **Ator:** Operador · **Issue:** #4
- **Pré-condição:** existe credencial de administrador configurada (senha inicial vinda do ambiente, `ADMIN_PASSWORD` ou equivalente — nome final é do Tech Lead).
- **Fluxo principal**
  1. Operador abre `/admin` sem sessão.
  2. Sistema redireciona para a tela de login.
  3. Operador informa usuário e senha.
  4. Sistema valida, cria sessão e grava cookie `HttpOnly`, `Secure`, `SameSite=Lax`.
  5. Sistema devolve o painel com a configuração efetiva.
- **Alternativos**
  - 4a. Credencial inválida: mensagem genérica, contador de tentativas por IP incrementado; nenhuma informação sobre qual campo errou.
  - 4b. Limite de tentativas atingido: bloqueio temporário com o tempo restante e registro do evento.
  - 4c. Senha ainda é a inicial: painel exige troca antes de liberar as seções.
- **Pós-condição:** sessão válida e auditada; nenhum dado de configuração servido antes da autenticação.

### UC-CFG-02 — Consultar configuração efetiva

- **Ator:** Operador · **Issues:** #6, #3, #7
- **Pré-condição:** sessão válida.
- **Fluxo principal**
  1. Operador abre o painel ou uma seção.
  2. Sistema resolve, para cada campo, valor efetivo e metadados: tipo, padrão, atual, origem (`default` / `env` / `db`), sensível, exige reinício, valor pendente.
  3. Sistema substitui todo campo sensível por marcador e envia `is_set: true|false`.
  4. Painel renderiza o formulário a partir do schema, sem campo escrito à mão no HTML.
- **Alternativos**
  - 2a. Banco de overrides indisponível: sistema responde com a configuração de ambiente + padrão e sinaliza `overrides_unavailable: true`.
  - 2b. Override gravado para chave que não existe mais em `Settings`: campo aparece como órfão, não aplicado, com ação de remoção.
- **Pós-condição:** operador vê o estado ativo e a origem de cada valor.

### UC-CFG-03 — Alterar configuração

- **Ator:** Operador · **Issues:** #3, #6, #7
- **Pré-condição:** sessão válida; campo não é somente leitura.
- **Fluxo principal**
  1. Operador altera um ou mais campos e envia.
  2. Sistema valida o conjunto inteiro contra o schema.
  3. Sistema grava os overrides em transação única.
  4. Sistema devolve o valor efetivo (não o digitado), o diff e a lista de campos que exigem reinício.
  5. Sistema invalida a configuração em memória e registra `config_changed`.
- **Alternativos**
  - 2a. Um campo inválido: `422` com chave, motivo e valor recusado; **nenhum** campo é gravado.
  - 2b. Campo somente leitura enviado: `422` com `read_only`.
  - 2c. Segredo enviado como marcador `"***"`: ignorado, valor atual preservado.
  - 2d. Chave de criptografia ausente: escrita de segredo falha fechada (`503`), nada gravado.
- **Pós-condição:** configuração consistente, auditada, com pendência de reinício sinalizada quando houver.

### UC-CFG-04 — Restaurar padrão ou ambiente

- **Ator:** Operador · **Issues:** #3, #7
- **Fluxo principal:** operador aciona "restaurar" em um campo ou seção, confirma, sistema apaga o override correspondente e devolve o valor que passa a valer (`env` ou `default`), com a origem correta.
- **Pós-condição:** override removido, uma linha de auditoria por campo afetado.

### UC-CFG-05 — Testar conexão de um grupo

- **Ator:** Operador · **Issue:** #3, #7
- **Pré-condição:** sessão válida.
- **Fluxo principal**
  1. Operador aciona "testar conexão" em S3, Redis, YouTube API ou Meta.
  2. Sistema executa uma verificação **somente leitura**, com os valores atuais (inclusive os ainda não salvos na tela, quando o painel envia o rascunho).
  3. Sistema responde ok/falha com a mensagem do provedor e o tempo de resposta.
- **Alternativos:** 3a. Timeout: falha com `timeout` explícito, sem derrubar o painel. 3b. Credencial ausente: falha `not_configured`, sem tentar rede.
- **Pós-condição:** nada é gravado; a verificação não consome quota além de uma chamada de leitura.

### UC-CFG-06 — Aplicar mudança a quente

- **Ator:** Sistema · **Issue:** #5
- **Pré-condição:** campo classificado como quente.
- **Fluxo principal**
  1. Escrita concluída (UC-CFG-03).
  2. Sistema publica a nova versão da configuração.
  3. Componentes que dependem do campo passam a ler a versão vigente na requisição seguinte: TTL de cache, rate limit, concorrência, breaker, user agents, limites de scraping, `log_level`, credenciais opcionais.
- **Alternativos**
  - 3a. TTL alterado: entradas já em cache não mantêm o TTL antigo — cache é invalidado.
  - 3b. Campo frio alterado: nada é aplicado; fica como pendente de reinício (UC-CFG-07).
- **Pós-condição:** comportamento novo observável sem restart.

### UC-CFG-07 — Ver pendência de reinício

- **Ator:** Operador · **Issues:** #5, #7
- **Fluxo principal:** painel mostra aviso persistente com a lista de campos frios alterados e o comando exato para aplicar (`docker compose restart api` ou equivalente), até que o serviço reinicie e as pendências zerem.
- **Pós-condição:** operador sabe que o valor salvo não está em vigor.

### UC-CFG-08 — Consultar auditoria

- **Ator:** Operador · **Issue:** #4
- **Fluxo principal:** operador vê as últimas N alterações com data/hora, usuário, campo, valor anterior, valor novo e IP; segredo aparece como `alterado`, nunca em claro.
- **Pós-condição:** leitura apenas; nenhuma escrita.

### UC-CFG-09 — Subir com banco indisponível

- **Ator:** Sistema · **Issue:** #6
- **Fluxo principal:** boot não consegue ler overrides; serviço sobe com ambiente + padrão, registra aviso estruturado, `/v1/health` continua respondendo e o painel sinaliza modo degradado.
- **Pós-condição:** indisponibilidade do banco de configuração não impede a API pública de atender.

## 4. Requisitos funcionais

| ID | Requisito | Origem | Aceite |
|---|---|---|---|
| RF-CFG-01 | A configuração efetiva é resolvida em três níveis com precedência `db > env > default`. | #6 | CA-CFG-01 |
| RF-CFG-02 | Cada campo tem classificação declarada: `sensível`, `quente`, `frio` (exige reinício), `somente leitura`. | #6, #5 | CA-CFG-02 |
| RF-CFG-03 | Campos somente leitura (`database_url`, `api_prefix`, `environment`, `cache_backend`, `redis_url`, backend e caminhos de cookies) recusam override por banco. | #6 | CA-CFG-03 |
| RF-CFG-04 | `get_settings()` devolve a versão efetiva e é invalidado explicitamente após escrita; nenhuma chamada existente muda de assinatura. | #6 | CA-CFG-04 |
| RF-CFG-05 | A API administrativa expõe `GET/PATCH/DELETE /admin/api/config`, `POST /admin/api/config/validate`, `POST /admin/api/config/test`, `GET /admin/api/config/schema`. | #3 | CA-CFG-05 |
| RF-CFG-06 | Escrita é atômica: ou todos os campos válidos entram, ou nenhum. | #3 | CA-CFG-06 |
| RF-CFG-07 | Nenhuma resposta, log ou trilha de auditoria contém valor de campo sensível; só marcador e `is_set`. | #3, #4 | CA-CFG-07 |
| RF-CFG-08 | Toda escrita gera exatamente uma linha de auditoria por campo alterado. | #4 | CA-CFG-08 |
| RF-CFG-09 | `/admin` e `/admin/api/*` exigem sessão; `/v1/*` permanece aberto como hoje. | #4 | CA-CFG-09 |
| RF-CFG-10 | Sessão em cookie `HttpOnly`, `Secure` (quando TLS), `SameSite=Lax`, com expiração e logout. | #4 | CA-CFG-10 |
| RF-CFG-11 | Escritas exigem proteção CSRF; login tem limite de tentativas por IP com bloqueio temporário. | #4 | CA-CFG-11 |
| RF-CFG-12 | Campos quentes passam a valer na requisição seguinte, sem restart, com troca atômica. | #5 | CA-CFG-12 |
| RF-CFG-13 | Campos frios nunca são aplicados em silêncio: ficam marcados como pendentes com o comando de reinício. | #5, #7 | CA-CFG-13 |
| RF-CFG-14 | O formulário é gerado de `GET /admin/api/config/schema`; nenhum campo é escrito à mão no HTML. | #7 | CA-CFG-14 |
| RF-CFG-15 | O painel não carrega nenhum recurso externo (sem CDN, sem fonte remota), no padrão de `install_docs` em `src/api/docs.py`. | #7 | CA-CFG-15 |
| RF-CFG-16 | Teste de conexão é somente leitura e não persiste nada. | #3 | CA-CFG-16 |
| RF-CFG-17 | Falha ao ler overrides no boot não impede o serviço de subir. | #6 | CA-CFG-09 |
| RF-CFG-18 | Override de chave inexistente em `Settings` não é aplicado e é listado como órfão removível. | #6 (lacuna) | CA-CFG-17 |

## 5. Requisitos não funcionais

| ID | Requisito | Número | Aceite |
|---|---|---|---|
| RNF-CFG-01 | `GET /admin/api/config` responde em ≤ 300 ms (p95) com o banco local. | 300 ms p95 | medido em teste de carga local |
| RNF-CFG-02 | Escrita de configuração conclui em ≤ 500 ms (p95), incluindo auditoria. | 500 ms p95 | idem |
| RNF-CFG-03 | Troca de configuração concorrente a requisições em voo não gera exceção nem estado misto: 100 requisições durante 10 trocas, zero erro 5xx. | 100 req / 10 trocas | teste de concorrência |
| RNF-CFG-04 | Painel utilizável em viewport de 360 px (uma coluna) e 1280 px (duas colunas). | 360 px / 1280 px | verificação manual registrada |
| RNF-CFG-05 | Nenhum segredo em claro em banco, log, resposta HTTP ou auditoria; verificado por teste que varre a saída. | zero ocorrência | teste automatizado |
| RNF-CFG-06 | Com `WEB_CONCURRENCY > 1`, a configuração efetiva é a mesma em todos os workers após a escrita. | todos os workers | teste com 2 workers |
| RNF-CFG-07 | Bloqueio de login após 5 tentativas erradas em 15 min, por IP. | 5 / 15 min | teste |

## 6. Critérios de aceite (Gherkin)

**CA-CFG-01 — precedência**
```gherkin
Cenário: override no banco vence o ambiente
  Dado que CACHE_TTL_PROFILE_SECONDS=900 no ambiente
  E existe override de cache_ttl_profile_seconds=1200 no banco
  Quando consulto a configuração efetiva
  Então o valor de cache_ttl_profile_seconds é 1200
  E a origem do campo é "db"
  Quando apago o override
  Então o valor volta a 900 e a origem é "env"
```

**CA-CFG-03 — somente leitura**
```gherkin
Cenário: campo somente leitura recusa override
  Quando envio PATCH com {"database_url": "sqlite+aiosqlite:///./outro.db"}
  Então a resposta é 422
  E o erro aponta o campo database_url com motivo "read_only"
  E nenhum override é gravado
```

**CA-CFG-06 — atomicidade**
```gherkin
Cenário: um campo inválido impede toda a gravação
  Quando envio PATCH com {"cache_ttl_profile_seconds": 600, "concurrency_instagram": 0}
  Então a resposta é 422 apontando concurrency_instagram
  E cache_ttl_profile_seconds continua com o valor anterior
```

**CA-CFG-07 — segredo nunca vaza**
```gherkin
Cenário: valor de segredo não aparece em nenhuma saída
  Dado que s3_secret_access_key está definido no ambiente
  Quando leio GET /admin/api/config com log em DEBUG
  Então o campo aparece com "***" e is_set=true
  E a string do valor real não aparece na resposta nem no log
```

**CA-CFG-12 — mudança a quente**
```gherkin
Cenário: TTL novo vale sem restart
  Dado cache_ttl_profile_seconds=900 em vigor
  Quando altero para 60 pela API administrativa
  E faço uma requisição de perfil que vai ao upstream
  E faço a mesma requisição de novo
  Então a segunda é servida do cache
  E após 60 segundos a entrada expira
```

**CA-CFG-13 — campo frio não aplica em silêncio**
```gherkin
Cenário: troca de redis_url fica pendente
  Quando altero redis_url pela API administrativa
  Então a resposta lista redis_url em "requires_restart"
  E o cache continua no backend anterior
  E GET /admin mostra a pendência com o comando de reinício
```

**CA-CFG-09 — boot degradado**
```gherkin
Cenário: banco de overrides indisponível
  Dado que a leitura de overrides falha na inicialização
  Quando o serviço sobe
  Então GET /v1/health responde 200
  E a configuração efetiva usa ambiente + padrão
  E existe um log de aviso com o motivo
```

## 7. Rastreabilidade

| Requisito | Caso de uso | Componente (código) | Contrato | Teste |
|---|---|---|---|---|
| RF-CFG-01..04 | UC-CFG-02, 09 | `src/core/config.py`, novo resolvedor | — | `tests/test_config_precedence.py` |
| RF-CFG-05..07, 16 | UC-CFG-02, 03, 05 | `src/api/routes/admin.py` | OpenAPI `/admin/api/config*` | `tests/test_admin_config.py` |
| RF-CFG-08, 09, 10, 11 | UC-CFG-01, 08 | `src/api/security.py`, `src/db/models.py` | `/admin/api/audit` | `tests/test_admin_auth.py` |
| RF-CFG-12, 13 | UC-CFG-06, 07 | `src/core/{cache,ratelimit,breaker,http}.py`, `src/platforms/base.py` | — | `tests/test_hot_reload.py` |
| RF-CFG-14, 15 | UC-CFG-02 | `src/api/docs.py` (padrão), assets `/admin-assets` | `GET /admin/api/config/schema` | verificação manual registrada |
| RF-CFG-17, 18 | UC-CFG-09 | `src/main.py` (lifespan) | `/v1/health` | `tests/test_boot_degraded.py` |

## 8. Lacunas apontadas (requisito que falta no backlog)

1. **Contrato de `/v1/health` ampliado não tem issue.** #7 pede "ver refletido em `/v1/health`" e #11 pede estado de conta Meta ali, mas nenhuma issue especifica os campos novos nem garante que o healthcheck do `docker-compose.yml` continue barato. Precisa de RF aditivo e teste de que `/v1/health` não faz chamada de rede.
2. **Propagação entre workers não está coberta.** #6 cita `WEB_CONCURRENCY > 1` no aceite, #5 não. Sem um requisito explícito de propagação, cada worker teria cache divergente. Ver RNF-CFG-06.
3. **Não há requisito de "config efetiva legível sem segredo" para diagnóstico.** Operador não consegue provar qual valor está em vigor após um incidente. Sugiro `GET /admin/api/config?reveal=non_secret` já coberto por RF-CFG-05/07 — explicitar no contrato.
4. **Nenhuma issue trata a ordem entre as duas frentes.** `META_APP_ID`, `META_APP_SECRET`, versão da Graph API e URL de callback são configuração do painel (#3/#6) e pré-requisito do OAuth (#10). Se a Meta entrar primeiro, esses campos nascem fora do painel.
5. **Falta requisito de migração de override órfão.** Código que remove um campo deixa linha no banco. Coberto por RF-CFG-18, mas precisa decisão de quando limpar.
6. **Falta decisão de TLS/ingress.** #4 deixa "a confirmar" se o serviço está atrás de proxy reverso do Coolify. Sem isso, o requisito de cookie `Secure` não é verificável em produção.
7. **Falta requisito de retenção da auditoria.** Nenhuma issue define por quanto tempo a trilha fica. Sugiro 90 dias com expurgo, mas é decisão do usuário.

## 9. Em aberto

- Nome final das variáveis de bootstrap do administrador (senha inicial e chave de criptografia) — Tech Lead.
- Se a configuração quente usa um objeto versionado lido por requisição ou um publicador de eventos — Arquiteto.
- Confirmar se há proxy reverso com TLS em produção — Leonardo/Coolify.
- Definir retenção da auditoria.
