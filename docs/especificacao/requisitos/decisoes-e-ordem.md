# Decisões fechadas das seções "Aberto" e ordem de implementação

Autor: Analista de Requisitos · 2026-11-17
Escopo: fecha o que é decisão de **requisito**. O que é biblioteca, algoritmo ou infraestrutura fica marcado com o dono.

## Épico 2 — Painel de configuração

| Issue | Pergunta em aberto | Decisão | Dono se não for requisito |
|---|---|---|---|
| #6 | Segredos no banco ou só presença no `.env`? | **Gravados cifrados no banco.** Sem isso a premissa "configurar sem editar arquivo na VPS" falha para S3, Meta e YouTube. A chave de criptografia vem do ambiente e nunca do banco. Sem chave, escrita de segredo falha fechada. | algoritmo/chave: Tech Lead |
| #6 | Versionar histórico de overrides com rollback? | **Não nesta entrega.** A auditoria de #4 já guarda valor anterior e novo, o que permite rollback manual. Versionamento dedicado fica para depois. | — |
| #3 | Prefixo `/admin/api` ou `/v1/admin`? | **`/admin/api`.** `/v1` é a API pública e `api_prefix` é campo frio e configurável; o painel não pode mudar de URL quando o prefixo mudar. | — |
| #3 | Teste de conexão pode escrever no bucket? | **Nunca escreve.** Só leitura: `head` de bucket/objeto no S3, `PING` no Redis, chamada de leitura mínima no YouTube e `/me` na Meta. | — |
| #7 | Tema escuro? | **Não.** Layout claro, uma coluna abaixo de 720 px. Tema escuro fora de escopo. | — |
| #7 | Só o Leonardo ou clientes? | **Um único operador administrador.** Multi-usuário e multi-cliente fora de escopo; o modelo de dados não precisa prever papéis agora. | — |
| #5 | Recriar `HttpClient` em runtime? | **Não.** `proxy_url`, timeouts e `http_max_connections` passam a **frios** nesta entrega. Recriar o pool com requisições em voo é risco sem retorno imediato; a tela mostra "exige reinício". | confirmação: Arquiteto/Tech Lead |
| #4 | Um usuário ou vários? | **Um usuário.** Senha inicial por variável de ambiente, troca obrigatória no primeiro acesso. | hash: Tech Lead |
| #4 | TLS atrás de proxy do Coolify? | **Requisito:** cookie `Secure` sempre que `environment=prod`; se não houver TLS detectável, o painel não libera login em produção (falha fechada) e registra o motivo. | **a confirmar** com Leonardo/Coolify |

## Épico 1 — Meta

| Issue | Pergunta em aberto | Decisão | Dono se não for requisito |
|---|---|---|---|
| #10 | Só contas do Leonardo ou multi-tenant? | **Single-tenant nesta entrega.** O registro de conta já carrega identificador próprio para permitir multi-tenant depois, mas sem UI nem convite de cliente. | modelo: Modelador |
| #10 | App Meta existe? Produtos habilitados? | **A confirmar (MetaNO).** Bloqueante para conectar, não para especificar. | MetaNO |
| #10 | Permissões exatas e App Review? | **A confirmar (MetaNO).** Enquanto não aprovado, o sistema opera com fallback de scraping e `warnings` — já coberto por RF-META-06. | MetaNO |
| #10 | Rotação da chave de criptografia nesta entrega? | **Não nesta entrega, mas o registro guarda `key_id`.** Sem isso, rotação futura exige migração de dados. | Tech Lead |
| #8 | Instagram pessoal fica só no scraping? | **Sim.** API oficial responde `unsupported_account_type` para conta pessoal, sem tentativa. | MetaNO confirma |
| #8 | Janela padrão de Insights: 7, 28 ou 30? | **28 dias como padrão**, sempre sobrescrevível por parâmetro explícito na chamada. Se o MetaNO indicar outro padrão vigente, muda o default, não o requisito. | MetaNO |
| #8 | Guardar Insights no histórico tem restrição de política? | **A confirmar (MetaNO).** Até confirmar: guardar com `source`, e a gravação é desligável por configuração (`enable_meta_history`). | MetaNO |
| #12 | Só leitura ou também pausar/ajustar orçamento? | **Só leitura, definitivo nesta entrega.** Todos os endpoints `GET`; teste verifica que nenhum método diferente de `GET` é emitido. | — |
| #12 | Moeda e fuso: da conta ou forçar São Paulo? | **Da conta Meta, sempre ecoados na resposta.** Nunca converter. Forçar São Paulo produziria número que não bate com o Gerenciador de Anúncios. | — |
| #12 | Quais métricas no resumo padrão? | **spend, impressions, clicks, reach, frequency, ctr, cpc, cpm** mais o resultado do objetivo da campanha. Os nomes de ação por objetivo dependem do MetaNO; o resumo lista só o que a conta devolver, com `null` + `warnings` para o resto. | MetaNO |
| #9 | Quais campos de webhook assinar? | **Só o que gerar métrica de perfil/post.** Começar por `feed` da Página. `comments` e `mentions` do Instagram não geram métrica de perfil — fora de escopo até haver requisito. Lista final depende do MetaNO. | MetaNO |
| #9 | Precisa de URL pública com TLS? | **A confirmar (infra).** Bloqueia ativar, não implementar: o endpoint sobe e fica inerte sem domínio público configurado. | Leonardo/Coolify |
| #9 | Fila em banco ou Redis? | **Banco (SQLite).** Uma fila só, na mesma fonte de verdade, com worker único no `lifespan`. Redis não traz ganho para este volume e adiciona estado fora do histórico. | confirmação: Arquiteto |
| #11 | Limite conservador ou dinâmico? | **Os dois:** teto conservador próprio como piso e ajuste pelo cabeçalho de uso quando a Meta enviar. Se o MetaNO não confirmar o cabeçalho, vale só o teto. | MetaNO |
| #11 | Alertar por WhatsApp em bloqueio? | **Fora de escopo.** Vira issue separada com o agente que opera WhatsApp. | — |

## Lacunas de requisito (precisam de issue nova ou de escopo ampliado)

1. **Handle → conta Meta.** Sem requisito de vínculo `@handle` ↔ `ig_user_id` / Página, o UC-META-04 não é implementável. Nova issue ou ampliar #8.
2. **Descoberta de conta de anúncios.** #12 pressupõe a conta conectada; ninguém especifica descobrir e escolher entre várias. Ampliar #10.
3. **Exclusão de dados ao desconectar.** Definir o que acontece com snapshots e insights já gravados.
4. **Retenção** de eventos de webhook e da trilha de auditoria.
5. **Propagação de config quente entre workers** (`WEB_CONCURRENCY > 1`).
6. **Contrato aditivo de `/v1/health`** (config, contas Meta) sem perder o healthcheck barato do `docker-compose.yml`.
7. **Teste de conexão Meta** no painel: o que exatamente é chamado.
8. **Exposição pública do webhook** (domínio/TLS/ingress).

## Ordem de implementação recomendada

Regra: cada item entrega valor sozinho e desbloqueia o seguinte. Dependência dura em negrito.

**Trilha A — Configuração (começa primeiro)**
1. **#6** camada persistida + classificação dos campos. Desbloqueia todo o resto; não tem dependência.
2. **#4** autenticação + auditoria. **Depende de #6** para a tabela de auditoria e para o boot degradado.
3. **#3** API administrativa. **Depende de #4** (não pode nascer aberta) e de #6.
4. **#5** aplicação a quente. **Depende de #3** para existir gatilho de escrita; é o que dá sentido à tela.
5. **#7** painel web. **Depende de #3** (schema e endpoints) e de #5 (selo de pendência).

**Trilha B — Meta (paralela na preparação, sequencial na entrega)**
0. **Pré-requisito externo, dia 0:** confirmar app Meta, produtos, escopos e App Review com MetaNO. Tem prazo de terceiro e não bloqueia código de especificação.
6. **#10** OAuth + cofre. **Depende de #6** (campos `META_*` já nascem no painel, evitando retrabalho) e do parecer do MetaNO.
7. **#11** quota, backoff e breaker por conta. **Depende de #10** (precisa de token para exercitar) e dos limites confirmados.
8. **#8** adapter de perfil, posts e Insights. **Depende de #10** (token) e #11 (proteção de quota). Entrega o primeiro valor visível da frente Meta.
9. **#12** Marketing API. **Depende de #10, #11 e #8** (normalização e contrato de métricas já estáveis).
10. **#9** webhooks. **Depende de #8** (gravar snapshot), #11 (retentativa) e da exposição pública com TLS.

**Paralelismo seguro:** #7 (tela) pode andar junto com #10 (backend Meta) porque só consome o schema de configuração. #12 pode ser especificado em paralelo, mas implementado depois de #8.

**Caminho crítico:** #6 → #4 → #3 → #5 → #7. Se a Meta for prioridade de negócio, o caminho crítico vira #6 → #10 → #11 → #8, e #4/#3/#5 andam em paralelo.
