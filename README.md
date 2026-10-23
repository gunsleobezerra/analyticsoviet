# analyticsoviet

API de métricas de redes sociais que extrai dados públicos de **YouTube, TikTok, Instagram e Facebook**
usando apenas **HTML e JSON embutido** — nenhum navegador headless é iniciado.

```
GET  /v1/health
GET  /v1/platforms
POST /v1/scrape
GET  /v1/profile/{platform}/{handle}
GET  /v1/post?url=...
GET  /v1/metrics/{platform}/{handle}
GET  /v1/history/{platform}/{handle}
```

## Como funciona

Cada plataforma tem um adapter que segue o mesmo contrato (`src/platforms/base.py`):

1. `detect(url)` — resolve a URL em `{platform, kind, handle, resource_id}`.
2. `fetch_profile()` / `fetch_post()` — busca HTML com `httpx` (HTTP/2, UA rotativo, cookie jar opcional)
   e extrai os dados de **JSON embutido** (`ytInitialData`, `__UNIVERSAL_DATA_FOR_REHYDRATION__`,
   `data-sjs`) ou de **Open Graph tags**, conforme a plataforma.
3. Normalização para um contrato único (`src/models/schemas.py`) e cálculo de métricas derivadas.

Nada de JS é executado. O que não estiver no HTML inicial não é coletado — por isso cada resposta
declara `status`, `source`, `confidence` e `warnings`.

## Matriz de capacidades

| Plataforma | Perfil | Posts | Confiabilidade | Fonte principal |
|---|---|---|---|---|
| YouTube | followers, posts | views, likes, comments | **alta** | `ytInitialData` + `ytInitialPlayerResponse`, fallback `yt-dlp` e Data API v3 |
| TikTok | followers, following, likes, posts | views, likes, comments, shares | **média-alta** | `__UNIVERSAL_DATA_FOR_REHYDRATION__`, fallback oEmbed |
| Instagram | followers, following, posts | likes, comments | **média** | Open Graph tags (+ `data-sjs` quando presente) |
| Facebook | followers/likes (parcial) | reações/comentários (limitado) | **baixa** | `mbasic.facebook.com` + Open Graph |

`GET /v1/platforms` devolve essa matriz de forma machine-readable.

## Instalação

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
cp .env.example .env
```

Opcional (recomendado para YouTube, muito mais estável):

```bash
uv pip install --python .venv/bin/python -e ".[ytdlp]"
```

## Executar

```bash
uv run --python .venv/bin/python uvicorn main:app --app-dir src --reload --port 8000
```

Docs interativas em `http://localhost:8000/docs`.

### Exemplos

```bash
# perfil + últimos posts
curl "http://localhost:8000/v1/profile/youtube/@mkbhd?posts_limit=10"

# métricas derivadas (taxa de engajamento, médias, top posts)
curl "http://localhost:8000/v1/metrics/tiktok/@khaby.lame"

# vídeo/post único
curl --get "http://localhost:8000/v1/post" \
  --data-urlencode "url=https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# lote (paralelo, resultados parciais preservados)
curl -X POST "http://localhost:8000/v1/scrape" -H 'content-type: application/json' -d '{
  "urls": [
    "https://www.instagram.com/nasa/",
    "https://www.facebook.com/nasa",
    "https://www.tiktok.com/@nasa"
  ],
  "posts_limit": 6
}'

# série temporal gravada localmente
curl "http://localhost:8000/v1/history/instagram/nasa?days=30"
```

## Contrato de resposta

```jsonc
{
  "platform": "instagram",
  "kind": "profile",
  "url": "https://www.instagram.com/nasa/",
  "status": "partial",           // ok | partial | failed
  "source": "html",              // html | embedded_json | official_api | oembed | ytdlp
  "confidence": "medium",        // high | medium | low
  "cached": false,
  "profile": { "handle": "nasa", "metrics": { "followers": 97000000, "posts": 4300 } },
  "posts": [ { "url": "...", "metrics": { "likes": 12000 } } ],
  "derived": { "engagement_rate": 0.0042, "top_posts": [] },
  "warnings": ["metric unavailable: following"]
}
```

Uma falha de plataforma **nunca** derruba o lote: `POST /v1/scrape` retorna HTTP 200 com
`status: "failed"` e `error` naquele item, e os demais seguem normalmente.

## Cookies (opcional, aumenta muito a cobertura)

Facebook e Instagram bloqueiam tráfego anônimo com frequência. Exporte os cookies no formato
Netscape e aponte o caminho:

```bash
cookies/instagram.txt   # ou defina COOKIES_INSTAGRAM_FILE
cookies/facebook.txt    # ou defina COOKIES_FACEBOOK_FILE
```

O cookie jar é carregado por plataforma em `src/core/http.py` e reutilizado entre requisições.

## Configuração

Todas as variáveis estão em `.env.example`. Destaques:

| Variável | Padrão | Efeito |
|---|---|---|
| `CACHE_BACKEND` | `memory` | `memory` ou `redis` |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/analyticsoviet.db` | troque por Postgres para produção |
| `ENABLE_HISTORY` | `true` | grava snapshots para `/v1/history` |
| `PROXY_URL` | — | proxy HTTP/SOCKS para contornar bloqueio por IP |
| `RATELIMIT_<PLATFORM>_PER_MINUTE` | 15–60 | teto de requisições upstream |
| `CONCURRENCY_<PLATFORM>` | 2–8 | requisições simultâneas por plataforma |
| `YOUTUBE_API_KEY` | — | ativa fallback da Data API v3 |
| `ENABLE_YTDLP_FALLBACK` | `true` | fallback do YouTube sem browser |

## Testes

```bash
uv run --python .venv/bin/python pytest -q            # offline, usa fixtures HTML
uv run --python .venv/bin/python ruff check .
uv run --python .venv/bin/python mypy src
LIVE=1 uv run --python .venv/bin/python pytest -q -m live   # opcional, rede real
```

Os testes de parser rodam contra HTML salvo em `tests/fixtures/`. Para atualizar uma fixture
depois de uma mudança de layout upstream:

```bash
uv run --python .venv/bin/python scripts/capture_fixture.py <url> <nome-da-fixture>
```

## Limitações conhecidas

- **Sem JS**: dados carregados por XHR após o load inicial não são acessíveis.
- **Instagram**: contagem de likes/comentários por post vem de `og:description` e pode faltar;
  carrosséis e reels têm cobertura irregular.
- **Facebook**: métricas de páginas de terceiros são as mais frágeis. Para dados confiáveis de uma
  página que você administra, use a Graph API oficial.
- **TikTok**: algumas regiões recebem payload reduzido; sem cookies a taxa de sucesso cai.
- **Bloqueio por IP**: datacenter IPs são barrados por IG/FB. Use `PROXY_URL` residencial se
  precisar de escala.

## Uso responsável

Este projeto coleta apenas dados públicos. Respeite os Termos de Serviço de cada plataforma, o
`robots.txt`, a LGPD/GDPR e limites de taxa. Não use para perfilamento de pessoas físicas sem base
legal. Para dados em escala ou de contas privadas, use as APIs oficiais.
