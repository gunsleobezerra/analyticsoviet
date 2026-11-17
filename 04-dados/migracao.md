# Plano de migração

Banco: SQLite, arquivo `./data/analyticsoviet.db` no volume `api-data` (`docker-compose.yml`).
Hoje: `Base.metadata.create_all` no `lifespan` (`src/db/session.py`), sem controle de versão.
Isso funciona para tabela nova e **falha para coluna nova**: `create_all` não altera tabela existente. A migração 005 é justamente uma alteração de tabela, então o controle de versão deixa de ser opcional.

## Mecanismo (fechado pelo Tech Lead)

Runner próprio em SQL, sem Alembic. Os arquivos vão para `src/db/migrations/NNN-*.sql` (dentro do pacote, para viajar na imagem Docker, que copia só `src/`). **Os arquivos deste diretório são a cópia de referência**: o conteúdo precisa ser idêntico ao de `src/db/migrations/`, e o `checksum` gravado em `schema_migrations` é o que detecta divergência. Manter duas cópias é dívida; a saída é esta pasta virar só documentação e o SQL canônico ficar em `src/db/migrations/` — decisão do Tech Lead.

Requisitos não negociáveis:

1. Roda **uma vez por banco**, no entrypoint do container, antes do uvicorn (`python -m db.migrate && uvicorn ...`), com `flock` para que dois containers não migrem juntos. Com `WEB_CONCURRENCY > 1`, migrar dentro do `lifespan` faz N workers disputarem o mesmo `ALTER TABLE`.
2. Cada arquivo em **uma transação** (`BEGIN IMMEDIATE`); falhou, desfaz o arquivo inteiro e o boot aborta com log claro.
3. `schema_migrations` é a trava: `INSERT INTO schema_migrations` da versão logo antes do `COMMIT` (PK única impede dupla aplicação).
4. Banco sem `schema_migrations` e com `profile_snapshots` já existente = baseline aplicado; registra versão 1 sem tocar em dados.
5. Guarda de downgrade: se o banco tem versão maior que o código em execução, aborta o boot em vez de rodar com schema desconhecido.
6. Teste de paridade: banco novo por `create_all` e banco migrado precisam ter o mesmo `PRAGMA table_info` e os mesmos índices, para cada tabela.
7. Banco **novo** não executa 001–005: o runner detecta que não há tabelas, aplica `create_all` a partir dos modelos e carimba todas as versões como aplicadas. Rodar 005 num banco criado por `create_all` falha com `duplicate column name`, porque as colunas já existem. Essa é a diferença prática entre "banco novo" e "banco existente" e precisa estar no código, não na memória de quem opera.
8. Reaplicação: 001–004 são protegidos por `IF NOT EXISTS`; 005 é `ALTER TABLE` e **não** é idempotente. A única trava é `schema_migrations`. Teste de idempotência roda o runner duas vezes e exige a segunda execução sem efeito.

## Etapas

| # | Ação | Derruba serviço? | Reversão |
|---|---|---|---|
| 0 | Cópia do arquivo `.db` (operador, não eu) para fora do volume; anotar o commit em execução | não | restaure a cópia |
| 1 | `001-baseline.sql`: cria `schema_migrations`, registra versão 1 | não | nada a desfazer (tabela vazia de dados) |
| 2 | `002-config-admin.sql`: `admin_users` **primeiro**, depois `config_state`, `config_overrides`, `config_secrets`, `admin_sessions`, `admin_auth_attempts`, `config_audit_log` | não | `DROP TABLE` na ordem inversa das FKs |
| 3 | `003-api-keys.sql`: `api_keys`, `api_key_scopes` | não | `DROP TABLE api_key_scopes; DROP TABLE api_keys;` |
| 4 | `004-meta.sql`: conexão, escopos, ativos, tokens, eventos, webhooks, uso | não | `DROP TABLE` na ordem inversa das FKs (eventos → uso → tokens → assets → scopes → connections) |
| 5 | `005-snapshots.sql`: 6 `ADD COLUMN` nullable + 2 índices em `profile_snapshots` | **sim, por segundos** | ver abaixo |
| 6 | Carga inicial do administrador (usuário único, senha do ambiente) — fora do DDL, é seed da aplicação | não | apaga a linha |
| 7 | `PRAGMA foreign_keys=ON` por conexão (código, `src/db/session.py`) | não | remover o listener |

### Ordem dentro do 002 (correção de bloqueio da revisão)

A primeira versão criava `config_state` antes de `admin_users`, com FK para ela. Com `PRAGMA foreign_keys=ON`, criar a tabela filha antes da pai deixa a FK apontando para tabela inexistente e o `INSERT` seguinte em `config_state` falha com "no such table: main.admin_users". Ordem correta no arquivo: `admin_users` → `config_state` → `config_overrides` → `config_secrets` → `admin_sessions` → `admin_auth_attempts` → `config_audit_log`. Verificado em banco descartável com `foreign_keys=ON` e `PRAGMA foreign_key_check` vazio ao final.

### Por que a etapa 5 pede janela curta

`ALTER TABLE ... ADD COLUMN` no SQLite é O(1) quando a coluna é nullable e sem default — não reescreve a tabela. Com 108 000 linhas o comando termina em milissegundos. O que exige atenção é o **lock de escrita**: enquanto o `ALTER` roda, quem tenta gravar recebe `SQLITE_BUSY`. Com `busy_timeout=5000` e uma janela combinada de 5 minutos, o impacto é nulo na prática. Ainda assim: **não** há `DROP`/recriação de tabela nesta entrega; nada aqui reescreve `profile_snapshots`.

### Etapa 5 — reversão

As colunas novas são nullable e o código antigo as ignora. Reverter o deploy **é suficiente**; não é preciso `DROP COLUMN`. Se a decisão for limpar:

```sql
DROP INDEX IF EXISTS ux_snapshot_dedupe;
DROP INDEX IF EXISTS ix_snapshot_asset;
ALTER TABLE profile_snapshots DROP COLUMN provider;
ALTER TABLE profile_snapshots DROP COLUMN dedupe_key;   -- SQLite >= 3.35
ALTER TABLE profile_snapshots DROP COLUMN inserted_by;
ALTER TABLE profile_snapshots DROP COLUMN period_end;
ALTER TABLE profile_snapshots DROP COLUMN period_start;
ALTER TABLE profile_snapshots DROP COLUMN meta_asset_id;
```

`DROP COLUMN` **falha** se a coluna estiver em índice — por isso os índices caem antes. Manter as colunas é a opção mais segura; só remover depois de 2 releases estáveis.

## Backfill

Nada de dado antigo precisa ser reescrito para o sistema funcionar. Três cargas opcionais, todas em lote:

### 1. `inserted_by` das linhas antigas (opcional)
`NULL` já significa "requisição" pela regra declarada. Só preencher se a leitura preferir valor explícito:

```sql
UPDATE profile_snapshots SET inserted_by = 'request'
WHERE inserted_by IS NULL AND id BETWEEN ? AND ?;   -- lotes de 5 000, com 50 ms entre lotes
```
Não é obrigatório; 108k linhas em lotes de 5k levam ~1 s no total.

### 2. `handle` em minúsculas (recomendado, antes de qualquer índice único)
Hoje `handle` é gravado como veio da plataforma (`src/db/snapshots.py` copia `profile.handle`). A busca em `fetch_history` é igualdade exata — `NASA` e `nasa` viram séries diferentes no gráfico. Antes de normalizar, **verificar duplicata**:

```sql
SELECT platform, lower(handle) AS h, count(DISTINCT handle) AS variantes
FROM profile_snapshots GROUP BY platform, lower(handle) HAVING variantes > 1;
```
Se voltar linha, é dado real divergente: decidir com o Leonardo se consolida (UPDATE no handle antigo para o canônico) ou mantém as séries separadas. **Não consolidar sem essa decisão** — é reescrita de dado que eu não criei.

```sql
UPDATE profile_snapshots SET handle = lower(handle)
WHERE handle <> lower(handle) AND id BETWEEN ? AND ?;   -- só depois da decisão
```

### 3. Verificação de órfãos antes de ligar `foreign_keys`
Com FK desligada até hoje (`create_all` não liga o pragma), pode existir referência inválida. Consulta de checagem após a 005:

```sql
SELECT count(*) FROM profile_snapshots s
LEFT JOIN meta_assets a ON a.id = s.meta_asset_id
WHERE s.meta_asset_id IS NOT NULL AND a.id IS NULL;
```
Tem de retornar 0. Se não retornar, é bug de escrita no adapter, não da migração.

## Pragmas obrigatórios (mudança de código, não de DDL)

Sem isso, o modelo não tem integridade referencial nenhuma — e a FK declarada no DDL vira decoração:

| Pragma | Valor | Por quê | Onde |
|---|---|---|---|
| `journal_mode` | `WAL` | leitura concorrente com escrita; persistente no arquivo, roda 1× | uma vez na migração |
| `foreign_keys` | `ON` | SQLite ignora FK por padrão, por conexão | listener em `src/db/session.py` |
| `busy_timeout` | `5000` ms | webhook + requisição disputando escrita | idem |
| `synchronous` | `NORMAL` | com WAL, troca durabilidade de último commit por velocidade; aceitável para métrica | idem |

`journal_mode=WAL` não pode ser ativado dentro de transação — executar isolado, no passo de migração.

## Concorrência de escrita com `WEB_CONCURRENCY > 1`

SQLite tem **um escritor**. Com N workers, cada um abre sua conexão:

- Escrita sempre em transação curta; proibido `SELECT` longo entre `BEGIN` e `COMMIT`.
- Worker de webhook: só **um** processo consome a fila. Com `WEB_CONCURRENCY > 1`, ou o worker fica desligado e a fila é consumida por rotina externa, ou o consumo usa claim atômico:

```sql
UPDATE meta_webhook_events
SET status = 'processing', processing_started_at = ?, attempts = attempts + 1
WHERE id = (SELECT id FROM meta_webhook_events
            WHERE status = 'pending' AND next_attempt_at <= ?
            ORDER BY next_attempt_at LIMIT 1)
RETURNING id;
```
`RETURNING` exige SQLite ≥ 3.35 (a imagem roda 3.40+; conferir com `python -c "import sqlite3; print(sqlite3.sqlite_version)"` antes do deploy).

- Erro `database is locked` depois do `busy_timeout`: repetir a transação até 3× com espera de 50/150/450 ms. Não aumentar o timeout global sem medir.

## Invariante de schema

Banco criado por `create_all` e banco migrado precisam ser idênticos. Teste de aceitação da migração:

```
PRAGMA table_info(profile_snapshots);      -- mesmas colunas, mesma ordem? (ordem difere: ADD COLUMN vai ao fim)
PRAGMA index_list(profile_snapshots);      -- mesmos índices
```
Ordem de coluna **vai** divergir se o modelo SQLAlchemy declarar as colunas novas no meio. Declarar as colunas novas **no fim** da classe `ProfileSnapshot` evita a diferença. Se preferir organizar por assunto, aceite a divergência e documente — o que não pode é `create_all` criar índice que a migração não cria.

## Verificação final (queries de aceite)

```sql
-- 1. versões aplicadas em ordem, sem buraco
SELECT version, name FROM schema_migrations ORDER BY version;

-- 2. nenhuma tabela nova ficou de fora
SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;

-- 3. FK ligada na sessão atual
PRAGMA foreign_keys;              -- deve retornar 1

-- 4. modo de journal
PRAGMA journal_mode;              -- deve retornar wal

-- 5. unicidade de token vigente respeitada
SELECT connection_id, COALESCE(asset_id,''), token_kind, count(*)
FROM meta_tokens WHERE invalid_at IS NULL
GROUP BY 1,2,3 HAVING count(*) > 1;   -- deve retornar vazio

-- 6. fila sem evento preso há mais de 10 minutos
SELECT count(*) FROM meta_webhook_events
WHERE status='processing' AND processing_started_at < datetime('now','-10 minutes');
```
