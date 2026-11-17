# Especificação de requisitos — Integração oficial com a Meta (Graph API + Marketing API)

- **Épico:** #1 · **Issues:** #10, #11, #8, #12, #9
- **Repo de código:** `gunsleobezerra/analyticsoviet` (clone de leitura em `Shared/analyticsoviet`)
- **Autor:** Analista de Requisitos · **Data:** 2026-11-17
- **Regra de fonte:** toda afirmação sobre a Meta depende do MetaNO. O que ele não confirmar aparece como **a confirmar** e não vira critério de aceite fechado.

## 1. Problema e objetivo

`src/platforms/instagram.py` lê Open Graph (confiabilidade média) e `src/platforms/facebook.py` lê `mbasic` (baixa). Ambos entregam seguidores e engajamento aproximados, nada de alcance, impressões, salvamentos ou demografia, e quebram sob bloqueio anônimo. Não existe gasto de anúncios em lugar nenhum.

**Objetivo:** a Graph API e a Marketing API passam a ser a fonte primária para Instagram profissional e Página do Facebook, com OAuth, token de longa duração, cofre cifrado, quota controlada, webhooks e fallback explícito para scraping quando não há conta conectada.

**Fora de escopo:** escrita na conta de anúncios (pausar, ajustar orçamento), Instagram pessoal via API oficial, multi-tenant com vários clientes conectando as próprias contas, alerta por WhatsApp.

## 2. Atores

| Ator | Descrição |
|---|---|
| **Operador** | Conecta e desconecta contas Meta no painel, vê estado de token em `/v1/health`. |
| **Meta (Graph/Marketing API)** | Origem oficial dos dados e emissora de webhooks. |
| **Consumidor da API** | Chama `/v1/profile/*`, `/v1/posts/*`, `/v1/meta/ads/*`. Recebe `source` e `confidence`. |
| **Agendador** | Rotina que consulta métricas periodicamente e consome quota. |

## 3. Casos de uso

### UC-META-01 — Conectar conta Meta

- **Ator:** Operador · **Issue:** #10
- **Pré-condição:** `META_APP_ID`, `META_APP_SECRET`, `META_GRAPH_VERSION`, `META_OAUTH_REDIRECT_URI` e chave de criptografia definidos; operador autenticado no painel.
- **Fluxo principal**
  1. Operador aciona "conectar conta Meta".
  2. Sistema gera URL de autorização com `state` assinado, de uso único e com validade curta, e redireciona o navegador.
  3. Operador autoriza no site da Meta.
  4. Meta redireciona para o callback com `code` e `state`.
  5. Sistema valida o `state`, troca `code` por token de curta duração e depois por token de longa duração.
  6. Sistema descobre as Páginas do usuário e o Instagram profissional vinculado a cada Página.
  7. Sistema grava conta, Página(s), Instagram vinculado, escopos concedidos, tipo e expiração do token, com o token cifrado.
  8. Sistema faz uma chamada de teste de leitura e mostra o resultado ao operador.
- **Alternativos**
  - 5a. `state` inválido, expirado ou já usado: callback recusado com `invalid_state`, nada gravado.
  - 5b. Operador nega a autorização: retorno com erro tratado, mensagem clara, nada gravado.
  - 6a. Nenhuma Página com Instagram profissional vinculado: conta é gravada como `conectada_sem_instagram`, com aviso.
  - 7a. Chave de criptografia ausente ou inválida: conexão abortada com falha fechada, nada gravado.
  - 8a. Chamada de teste falha: conta permanece gravada como `token_invalido`, com o motivo.
- **Pós-condição:** conta conectada e auditada; token recuperável apenas pelo código, nunca por API ou log.

### UC-META-02 — Desconectar conta Meta

- **Ator:** Operador · **Issue:** #10
- **Fluxo principal**
  1. Operador aciona "desconectar" em uma conta, com confirmação explícita.
  2. Sistema revoga o token na Meta, quando a Meta permitir.
  3. Sistema apaga o token cifrado e marca a conta como `desconectada`.
  4. Endpoints de Instagram/Facebook passam a responder por scraping, com aviso em `warnings`.
- **Alternativos:** 2a. Revogação falha na Meta: token é apagado localmente de qualquer forma e o evento é registrado.
- **Pós-condição:** nenhum token utilizável permanece no banco.

### UC-META-03 — Renovar token e detectar invalidez

- **Ator:** Sistema · **Issue:** #10
- **Fluxo principal**
  1. Sistema verifica a expiração dos tokens em intervalo configurado.
  2. Token perto do vencimento é renovado pelo mecanismo que o MetaNO confirmar para aquele tipo de token.
  3. Token inválido ou revogado na Meta (erro de autenticação na chamada) marca a conta como `desconectada`.
  4. `/v1/health` passa a listar, por conta, o status do token e os dias restantes.
- **Alternativos:** 2a. Renovação falha: conta marcada `expirada`, alerta em `/v1/health`, nenhum loop de retentativa.
- **Pós-condição:** estado do token sempre visível e consistente com a realidade da Meta.

### UC-META-04 — Consultar perfil, posts e Insights pela Graph API

- **Ator:** Consumidor da API · **Issue:** #8
- **Pré-condição:** existe conta conectada que cobre o handle pedido.
- **Fluxo principal**
  1. Consumidor chama `/v1/profile/instagram/{handle}` ou `/v1/profile/facebook/{handle}`.
  2. Sistema resolve a conta Meta correspondente e chama a Graph API com os campos explícitos necessários.
  3. Sistema normaliza para `src/models/schemas.py`, com `source: "official_api"` e `confidence: "high"`.
  4. Campos que a API oficial não entrega permanecem `null` com entrada em `warnings`.
  5. Sistema grava snapshot com a origem e devolve a resposta.
- **Alternativos**
  - 2a. Handle não pertence a nenhuma conta conectada: cai para scraping (UC-META-05).
  - 2b. Erro de permissão: não retenta; devolve o erro traduzido para `src/core/errors.py` com o código original preservado.
  - 2c. Handle é Instagram pessoal: `unsupported_account_type`, sem tentativa de API oficial.
- **Pós-condição:** resposta no contrato único, com procedência declarada.

### UC-META-05 — Cair para scraping

- **Ator:** Sistema · **Issue:** #8
- **Fluxo principal**
  1. Não há conta conectada, ou a chamada oficial falha por motivo não recuperável.
  2. Sistema usa o adapter de scraping já existente, sem alterar `detect()` de URL.
  3. Resposta sai com `source: "html"` e `warnings` dizendo o motivo da queda (`no_connected_account`, `meta_permission_denied`, `meta_rate_limited`, `meta_upstream_error`).
- **Pós-condição:** o endpoint nunca deixa de responder por falta de conta Meta.

### UC-META-06 — Consultar anúncios e gasto

- **Ator:** Consumidor da API · **Issue:** #12
- **Pré-condição:** conta de anúncios conectada e com permissão de leitura de anúncios.
- **Fluxo principal**
  1. Consumidor chama `GET /v1/meta/ads/{campaigns|adsets|ads|insights}`.
  2. Sistema resolve a conta de anúncios, monta a consulta com campos explícitos e chama a Marketing API.
  3. Sistema resolve a paginação por cursor até o limite de páginas e de tempo.
  4. Sistema devolve os itens com o período, a moeda e o fuso da conta ecoados na resposta.
- **Alternativos**
  - 3a. Limite de páginas atingido: devolve os itens obtidos com `truncated: true` e o cursor para continuação.
  - 2a. Conta sem permissão de anúncios: `meta_permission_denied` com mensagem acionável, serviço intacto.
  - 2b. Período inválido (fim antes do início, intervalo acima do máximo aceito): `422`.
- **Pós-condição:** somente leitura; nenhuma chamada de escrita na conta.

### UC-META-07 — Receber webhook da Meta

- **Ator:** Meta · **Issue:** #9
- **Fluxo principal**
  1. Meta chama `GET /v1/meta/webhook` para verificação; sistema devolve o desafio apenas se o token de verificação conferir.
  2. Meta envia `POST /v1/meta/webhook` com `X-Hub-Signature-256`.
  3. Sistema valida a assinatura antes de qualquer processamento.
  4. Sistema responde `200` em menos de 1 s e enfileira o evento.
  5. Worker identifica conta e objeto, busca o dado atualizado na Graph API e grava snapshot com origem `meta_webhook`.
- **Alternativos**
  - 3a. Assinatura inválida: `401`, nada gravado, nada enfileirado.
  - 4a. Payload acima do limite: `413`, evento descartado e registrado.
  - 5a. Evento duplicado (mesmo id): descartado, sem snapshot duplicado.
  - 5b. Objeto desconhecido: registrado e descartado sem erro.
  - 5c. Falha na Graph API: retentativa com backoff até o limite; depois `dead` com log.
- **Pós-condição:** métrica atualizada sem polling, com rastro do evento.

### UC-META-08 — Controlar quota e falhas por conta

- **Ator:** Sistema · **Issue:** #11
- **Fluxo principal**
  1. Antes de cada chamada, sistema aplica o limitador da conta.
  2. Após a chamada, sistema lê o cabeçalho de uso da Meta quando presente e ajusta o orçamento da janela.
  3. Erro de limite: backoff exponencial com jitter, respeitando o tempo indicado pela Meta.
  4. Erro de permissão: sem retentativa, sobe ao cliente traduzido.
  5. Falhas repetidas de uma conta abrem o breaker **daquela conta** apenas.
- **Pós-condição:** uma conta bloqueada não degrada as demais nem o scraping.

### UC-META-09 — Ver estado das contas Meta

- **Ator:** Operador · **Issue:** #11
- **Fluxo principal:** `/v1/health` mostra, por conta Meta, status do token, chamadas na janela corrente, estado do breaker e último erro (sem token nem app secret).
- **Pós-condição:** diagnóstico sem precisar de acesso ao banco.

## 4. Requisitos funcionais

| ID | Requisito | Origem | Aceite |
|---|---|---|---|
| RF-META-01 | Conexão por OAuth com `state` assinado, de uso único e com validade curta. | #10 | CA-META-01 |
| RF-META-02 | Token final é de longa duração e é gravado cifrado em repouso; nunca em claro em banco, log, resposta ou auditoria. | #10 | CA-META-02 |
| RF-META-03 | Token tem metadados: tipo, escopos concedidos, expiração, conta/Página, `key_id` de criptografia, status. | #10 | CA-META-03 |
| RF-META-04 | Renovação antes do vencimento; token inválido na Meta marca a conta como desconectada sem derrubar o serviço. | #10 | CA-META-04 |
| RF-META-05 | `DELETE /v1/meta/accounts/{id}` revoga na Meta e apaga o token local. | #10 | CA-META-05 |
| RF-META-06 | Endpoints de Instagram/Facebook usam Graph quando há conta conectada e scraping quando não há, sempre com `warnings` do motivo. | #8 | CA-META-06 |
| RF-META-07 | Instagram pessoal nunca usa API oficial: `unsupported_account_type`. | #8 | CA-META-07 |
| RF-META-08 | Campos ausentes continuam `null` + `warnings`; nunca zero. | #8 | CA-META-08 |
| RF-META-09 | `/v1/platforms` declara a capacidade oficial por plataforma. | #8 | CA-META-09 |
| RF-META-10 | Snapshots guardam a origem, e o histórico sinaliza troca de fonte. | #8 | CA-META-10 |
| RF-META-11 | Marketing API é somente leitura: todos os endpoints são `GET` e nenhuma chamada de escrita é emitida. | #12 | CA-META-11 |
| RF-META-12 | Paginação por cursor resolvida internamente, com limite de páginas e de tempo total e `truncated` explícito. | #12 | CA-META-12 |
| RF-META-13 | Resposta de insights ecoa período, moeda e fuso da conta; nenhuma conversão de moeda ou de fuso. | #12 | CA-META-13 |
| RF-META-14 | Campos pedidos explicitamente, sem `fields` curinga. | #12, #11 | CA-META-14 |
| RF-META-15 | Erro de limite dispara backoff com jitter; erro de permissão não é retentado. | #11 | CA-META-15 |
| RF-META-16 | Breaker é por conta Meta, não por plataforma. | #11 | CA-META-16 |
| RF-META-17 | Erros da Meta são traduzidos para `src/core/errors.py` preservando o código original. | #11 | CA-META-17 |
| RF-META-18 | `/v1/health` expõe, por conta, status do token, chamadas na janela e estado do breaker, sem segredo. | #11 | CA-META-18 |
| RF-META-19 | `GET /v1/meta/webhook` devolve o desafio apenas com o token de verificação correto. | #9 | CA-META-19 |
| RF-META-20 | `POST /v1/meta/webhook` valida `X-Hub-Signature-256` antes de qualquer processamento. | #9 | CA-META-20 |
| RF-META-21 | Webhook responde em menos de 1 s e processa em segundo plano, com idempotência por id de evento. | #9 | CA-META-21 |
| RF-META-22 | Evento de objeto desconhecido é registrado e descartado sem erro; payload acima do limite é recusado. | #9 | CA-META-22 |
| RF-META-23 | Nenhuma resposta de erro, log ou auditoria contém token ou app secret. | #10, #11 | CA-META-02 |
| RF-META-24 | Toda conta Meta registra o motivo da última falha, em campo legível, para o painel. | #10 (lacuna) | CA-META-03 |

## 5. Requisitos não funcionais

| ID | Requisito | Número | Aceite |
|---|---|---|---|
| RNF-META-01 | `POST /v1/meta/webhook` responde em < 1 s (p95) independentemente do processamento. | < 1 s p95 | teste de tempo |
| RNF-META-02 | Toda chamada à Meta tem timeout explícito e teto de tempo total por requisição de API. | ≤ 20 s por chamada | teste com `respx` atrasado |
| RNF-META-03 | Nenhum teste automatizado da suíte padrão toca a rede; `tests/live/` continua opt-in. | zero rede | `LIVE` desligado no CI |
| RNF-META-04 | Nenhum segredo em claro em banco, log, resposta ou auditoria, verificado por varredura automatizada. | zero ocorrência | teste |
| RNF-META-05 | Teto de páginas por consulta de anúncios: 10 páginas ou 15 s, o que vier primeiro. | 10 páginas / 15 s | teste |
| RNF-META-06 | Cache de chamada oficial tem TTL próprio, configurável, separado do TTL de scraping. | TTL próprio | teste |

## 6. Critérios de aceite (Gherkin)

**CA-META-01 — state**
```gherkin
Cenário: callback com state inválido é recusado
  Quando a Meta chama o callback com code válido e state expirado
  Então a resposta é 400 com código invalid_state
  E nenhuma conta é criada
```

**CA-META-02 — token nunca vaza**
```gherkin
Cenário: token não aparece em nenhuma saída
  Dado uma conta conectada com token de longa duração
  Quando leio /v1/health, a resposta de erro de uma chamada falha e o log em DEBUG
  Então nenhuma das saídas contém o valor do token nem o app secret
  E o dump da tabela de tokens mostra o valor cifrado, não o texto claro
```

**CA-META-04 — token revogado**
```gherkin
Cenário: token revogado na Meta não derruba o serviço
  Dado que a Meta responde erro de autenticação para o token da conta A
  Quando chamo /v1/profile/instagram/{handle de A}
  Então a resposta usa scraping com source "html"
  E warnings contém "meta_token_invalid"
  E /v1/health mostra a conta A como desconectada
  E /v1/health continua 200
```

**CA-META-06 — seleção de fonte**
```gherkin
Cenário: com conta conectada, a fonte é a Graph API
  Dado uma conta Instagram profissional conectada
  Quando chamo /v1/profile/instagram/{handle}
  Então source é "official_api" e confidence é "high"
  E os campos de Insights do período estão preenchidos ou null com warning
  Quando desconecto a conta e repito
  Então source é "html" e warnings contém "no_connected_account"
```

**CA-META-11 — somente leitura**
```gherkin
Cenário: nenhuma escrita na conta de anúncios
  Quando executo a suíte de testes dos endpoints de anúncios
  Então nenhuma requisição HTTP capturada usa método diferente de GET
```

**CA-META-12 — paginação**
```gherkin
Cenário: mais de 100 itens
  Dado que a Marketing API responde com 3 páginas de 50 itens
  Quando chamo /v1/meta/ads/insights
  Então a resposta contém os 150 itens
  E não há cursor pendente
  Quando a conta tem 12 páginas
  Então a resposta contém os itens das 10 primeiras
  E truncated é true com o cursor da próxima página
```

**CA-META-13 — moeda e fuso**
```gherkin
Cenário: período, moeda e fuso ecoados
  Dado uma conta de anúncios com moeda USD e fuso America/New_York
  Quando consulto insights de 2026-11-01 a 2026-11-07
  Então a resposta traz period.start, period.end, currency "USD" e timezone_name "America/New_York"
  E nenhum valor é convertido
```

**CA-META-15 — backoff e permissão**
```gherkin
Cenário: erro de limite espera antes de repetir
  Dado que a Meta responde erro de limite com espera indicada
  Quando a chamada é retentada
  Então a retentativa ocorre após o tempo calculado
  E o número de tentativas respeita o limite configurado

Cenário: erro de permissão não é retentado
  Dado que a Meta responde erro de permissão
  Quando a chamada falha
  Então houve exatamente uma tentativa
  E o cliente recebe erro com mensagem acionável e o código original preservado
```

**CA-META-16 — breaker por conta**
```gherkin
Cenário: uma conta bloqueada não afeta as outras
  Dado duas contas Meta conectadas
  Quando a conta A atinge o limiar de falhas
  Então o breaker de A abre
  E uma chamada para a conta B continua funcionando
```

**CA-META-19 — verificação do webhook**
```gherkin
Cenário: desafio só com token correto
  Quando a Meta chama GET /v1/meta/webhook com o token de verificação correto
  Então a resposta é 200 com o valor de hub.challenge
  Quando o token está errado
  Então a resposta é 403 e o desafio não é devolvido
```

**CA-META-21 — idempotência**
```gherkin
Cenário: evento duplicado não duplica snapshot
  Dado um evento já processado com id X
  Quando a Meta reenvia o evento X
  Então nenhum snapshot novo é criado
  E o descarte é registrado
```

## 7. Rastreabilidade

| Requisito | Caso de uso | Componente (código) | Contrato | Teste |
|---|---|---|---|---|
| RF-META-01..05, 23, 24 | UC-META-01, 02, 03 | `src/meta/oauth.py`, `src/meta/vault.py`, `src/db/models.py` | `GET /v1/meta/oauth/start`, `GET /v1/meta/oauth/callback`, `DELETE /v1/meta/accounts/{id}` | `tests/test_meta_oauth.py` |
| RF-META-06..10 | UC-META-04, 05 | `src/meta/adapter.py`, `src/platforms/registry.py`, `src/services/normalize.py`, `src/db/snapshots.py` | `/v1/profile/*`, `/v1/platforms` | `tests/test_meta_adapter.py`, `tests/live/` |
| RF-META-11..14 | UC-META-06 | `src/meta/marketing.py` | `/v1/meta/ads/*` | `tests/test_meta_ads.py` |
| RF-META-15..18 | UC-META-08, 09 | `src/meta/ratelimit.py`, `src/core/breaker.py`, `src/core/errors.py` | `/v1/health` | `tests/test_meta_quota.py` |
| RF-META-19..22 | UC-META-07 | `src/api/routes/meta_webhook.py`, worker no `lifespan` | `GET/POST /v1/meta/webhook` | `tests/test_meta_webhook.py` |

## 8. Lacunas apontadas (requisito que falta no backlog)

1. **Nenhuma issue trata exclusão de dados ao desconectar.** #10 apaga o token, mas snapshots e insights já gravados continuam. Precisa decisão: manter histórico anonimizado, manter com marca de origem, ou apagar. Hoje o comportamento é indefinido.
2. **Não há requisito de retenção de eventos de webhook.** #9 cria tabela de eventos sem prazo de expurgo — cresce sem limite no SQLite e vira dado pessoal parado.
3. **Não há requisito de "testar conexão Meta" no painel.** #3 lista "Meta" como grupo testável, mas nenhuma issue define o que o teste faz (leitura de `/me`? da conta de anúncios?). Precisa RF explícito, somente leitura.
4. **Nenhuma issue cobre a descoberta de contas de anúncios.** #10 descobre Páginas e Instagram; #12 pressupõe "conta de anúncios conectada", mas não há requisito de descobrir, escolher e persistir qual conta de anúncios usar quando o usuário tem várias.
5. **Não há requisito para o vínculo handle → conta Meta.** #8 fala em "conta conectada que cobre o handle", mas nada define como o sistema resolve `@handle` para `ig_user_id`. Sem isso o UC-META-04 não é implementável.
6. **Falta requisito de isolamento de quota entre consumidores.** #11 limita por conta, mas não diz o que acontece quando a rotina periódica consome toda a cota e um cliente pede na hora. Precisa prioridade ou reserva.
7. **Falta requisito de exposição pública do webhook.** URL pública com TLS depende de ingress (Coolify). Nenhuma issue cobre quem configura o domínio/rota. Bloqueante para ativar, não para implementar.
8. **Falta requisito de App Review e de ambiente de teste.** Se a MetaNO confirmar que os escopos exigem revisão, o serviço precisa operar em modo restrito enquanto o app não é aprovado — sem requisito, o fallback fica implícito.

## 9. Em aberto

- Versão da Graph API, escopos exatos, tipos de token, limites de quota e requisitos de webhook: **dependem do MetaNO**.
- Se o app Meta existe e quais produtos estão habilitados: a confirmar.
- Se guardar Insights no histórico tem restrição de política: a confirmar (MetaNO).
- Nome final dos campos de configuração da Meta em `Settings` — Tech Lead/Arquiteto.
- Algoritmo de criptografia do token e origem da chave — Tech Lead.
