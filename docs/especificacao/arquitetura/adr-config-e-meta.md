# ADRs — analyticsoviet (config + Meta)

Formato curto: decisão, alternativa descartada, consequência.

## ADR-0001 — Fonte oficial como estratégia dentro do adapter, não novo adapter

**Contexto:** `src/platforms/registry.py` mapeia `Platform -> adapter` (um por plataforma) e `detect()` define o contrato de URL.
**Decisão:** Graph API entra em `src/meta/sources.py`, injetada em `AdapterContext`; `InstagramAdapter`/`FacebookAdapter` escolhem a fonte e devolvem o mesmo `ScrapeResult`.
**Alternativa descartada:** `MetaAdapter` registrado para `instagram`/`facebook` — exigiria mudar `Platform`, `registry.get` e todo consumidor de URL.
**Consequência:** fallback trivial e contrato intacto; cada adapter acumula a lógica de escolha (mitigada por um helper comum em `base.py`).

## ADR-0002 — Proxy e timeouts são frios nesta entrega

**Contexto:** `httpx.AsyncClient` recebe proxy, timeout e limits na construção (`src/core/http.py.start()`); só existe `aclose()`.
**Decisão:** tratar `proxy_url` e `http_timeout_seconds` como campos frios, com selo "exige reinício". Rate limit, concorrência, breaker, TTL e user agents ficam quentes.
**Alternativa descartada:** recriar o cliente em runtime — requisição em voo pode ser fechada no meio, e o pool precisa de drenagem.
**Consequência:** proxy continua exigindo restart; em troca, nenhuma requisição quebra por mudança de config.

## ADR-0003 — Precedência banco > ambiente > padrão com objeto validado por inteiro

**Contexto:** `Settings` é pydantic-settings com `@lru_cache`; valores têm relação entre si (ex.: `cache_backend=redis` exige `redis_url` válida).
**Decisão:** `ConfigStore` monta o dicionário final, valida um `EffectiveSettings` completo e só então grava; nada de mutação de objeto existente.
**Alternativa descartada:** sobrescrever atributos de `Settings` — burla validação e `model_config` é imutável na prática.
**Consequência:** escrita atômica e 422 por campo; custo de reconstruir o objeto a cada mudança (raro).

## ADR-0004 — Um operador, senha no ambiente, sessão por cookie

**Contexto:** não há modelo de usuário nem autenticação em `src/api/deps.py`; o painel altera config e testa credenciais.
**Decisão:** hash de senha em `ADMIN_PASSWORD_HASH` (ambiente), sessão em tabela do SQLite, cookie `HttpOnly`/`SameSite=Lax`/`Secure` em prod, CSRF por header.
**Alternativa descartada:** tabela `users` desde já — sem segundo operador, aumenta superfície e migração.
**Consequência:** um único login; trocar senha é trocar variável. Multi-usuário fica para quando houver requisito.

## ADR-0005 — Fila de webhook em SQLite com lock de líder

**Contexto:** `uvicorn --workers N` executa o `lifespan` N vezes; Redis é opcional (`CACHE_BACKEND=memory`).
**Decisão:** eventos em tabela durável; processamento só no worker que detém a linha de lock com heartbeat; refetch na Graph e snapshot com origem.
**Alternativa descartada:** fila em Redis (perde evento em restart sem AOF confiável) e rotina em cada worker (processa N vezes).
**Consequência:** um processador por vez; throughput baixo é suficiente para eventos de conta própria.

## ADR-0006 — Segredos cifrados no banco apenas com chave do ambiente

**Contexto:** a tela precisa gravar `S3_SECRET_ACCESS_KEY`, `YOUTUBE_API_KEY` e tokens Meta, mas o `.env` é texto puro e o banco é um arquivo no volume.
**Decisão:** cifrar com chave simétrica vinda de `CONFIG_SECRET_KEY` / `META_TOKEN_ENCRYPTION_KEY`; sem chave, override de segredo é recusado e a tela mostra "definido no ambiente".
**Alternativa descartada:** guardar só "definido/não definido" — impede cadastrar credencial nova sem restart, que é o objetivo do painel.
**Consequência:** perda da chave inutiliza os segredos (não os dados); `key_version` no registro prepara rotação futura.
