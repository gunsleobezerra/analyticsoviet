"""Per-field metadata for every setting.

The panel, the admin API and the hot-apply step all need to know the same things
about a field: where it appears, how to render it, whether the database may
override it, and whether changing it takes effect at once. Keeping that in one
table means a new setting cannot be half-wired — a test asserts that every field
of :class:`Settings` has an entry here.

Class meanings:

``quente``
    Takes effect on the next request; nothing network-facing has to be rebuilt.
``fria``
    Needs a new process. The panel shows the value and a "restart required"
    badge, and never applies it silently.
``env_only``
    Only the environment may set it. A database override is refused, because
    these fields can turn authentication off or swap the key that decrypts every
    stored secret. Also cold by consequence.
``somente_leitura``
    Read-only even through the environment: shown for information, never written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from core.config import Settings


class FieldClass(StrEnum):
    """When a value takes effect, and who is allowed to write it."""

    HOT = "quente"
    COLD = "fria"
    ENV_ONLY = "env_only"
    READ_ONLY = "somente_leitura"


class FieldKind(StrEnum):
    """Input type, used to pick the control and to coerce a stored value."""

    STRING = "string"
    SECRET = "secret"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    ENUM = "enum"
    PATH = "path"
    LIST = "list"


class Section(StrEnum):
    """Panel grouping, in display order."""

    APP = "Aplicação"
    NETWORK = "Rede"
    CACHE = "Cache"
    RATELIMIT = "Limites por plataforma"
    CONCURRENCY = "Concorrência"
    BREAKER = "Circuit breaker"
    DATABASE = "Banco de dados"
    OFFICIAL_APIS = "APIs oficiais"
    COOKIES = "Cookies"
    SCRAPING = "Coleta"
    SECURITY = "Autenticação"


@dataclass(frozen=True)
class FieldSpec:
    """Everything the panel and the writer need to know about one field."""

    attribute: str
    section: Section
    label: str
    kind: FieldKind
    cls: FieldClass
    help: str
    sensitive: bool = False
    choices: tuple[str, ...] = field(default_factory=tuple)

    @property
    def name(self) -> str:
        """Environment variable / database key for this field."""
        return self.attribute.upper()

    @property
    def writable(self) -> bool:
        """Whether a value from the database may override the environment."""
        return self.cls in (FieldClass.HOT, FieldClass.COLD)

    @property
    def requires_restart(self) -> bool:
        """Whether a change needs a new process to take effect."""
        return self.cls in (FieldClass.COLD, FieldClass.ENV_ONLY)


def _spec(
    attribute: str,
    section: Section,
    label: str,
    kind: FieldKind,
    cls: FieldClass,
    help_text: str,
    *,
    sensitive: bool = False,
    choices: tuple[str, ...] = (),
) -> FieldSpec:
    return FieldSpec(
        attribute=attribute,
        section=section,
        label=label,
        kind=kind,
        cls=cls,
        help=help_text,
        sensitive=sensitive,
        choices=choices,
    )


#: Every setting the application reads, with the metadata the panel needs.
FIELDS: tuple[FieldSpec, ...] = (
    # ---- aplicação ----
    _spec("app_name", Section.APP, "Nome do serviço", FieldKind.STRING, FieldClass.READ_ONLY,
          "Nome exibido na raiz da API e nos logs."),
    _spec("app_version", Section.APP, "Versão", FieldKind.STRING, FieldClass.READ_ONLY,
          "Versão do build; vem do pacote e não é configurável."),
    _spec("environment", Section.APP, "Ambiente", FieldKind.ENUM, FieldClass.COLD,
          "dev, test ou prod. Muda o formato do log e as regras de cookie seguro.",
          choices=("dev", "test", "prod")),
    _spec("log_level", Section.APP, "Nível de log", FieldKind.STRING, FieldClass.HOT,
          "DEBUG, INFO, WARNING ou ERROR. Vale a partir da requisição seguinte."),
    _spec("api_prefix", Section.APP, "Prefixo da API", FieldKind.STRING, FieldClass.COLD,
          "Prefixo das rotas públicas, /v1 por padrão. Exige reinício."),
    _spec("public_base_url", Section.APP, "URL pública", FieldKind.STRING, FieldClass.COLD,
          "URL absoluta anunciada no OpenAPI. Vazio usa a do próprio pedido."),

    # ---- rede ----
    _spec("http_timeout_seconds", Section.NETWORK, "Timeout total (s)", FieldKind.FLOAT,
          FieldClass.COLD, "Tempo máximo de uma requisição HTTP. Exige reinício."),
    _spec("http_connect_timeout_seconds", Section.NETWORK, "Timeout de conexão (s)",
          FieldKind.FLOAT, FieldClass.COLD,
          "Tempo máximo para abrir a conexão. Exige reinício."),
    _spec("http_max_connections", Section.NETWORK, "Conexões máximas", FieldKind.INTEGER,
          FieldClass.COLD, "Teto de conexões abertas no pool HTTP. Exige reinício."),
    _spec("http_max_keepalive_connections", Section.NETWORK, "Conexões keep-alive",
          FieldKind.INTEGER, FieldClass.COLD,
          "Quantas conexões ociosas ficam abertas para reuso. Exige reinício."),
    _spec("http_retries", Section.NETWORK, "Tentativas por requisição", FieldKind.INTEGER,
          FieldClass.HOT, "Quantas vezes uma requisição é repetida antes de falhar."),
    _spec("http_retry_backoff_seconds", Section.NETWORK, "Espera entre tentativas (s)",
          FieldKind.FLOAT, FieldClass.COLD,
          "Base do intervalo crescente entre tentativas. Exige reinício."),
    _spec("proxy_url", Section.NETWORK, "Proxy", FieldKind.STRING, FieldClass.COLD,
          "Proxy de saída para o scraping, ex. http://host:porta. Exige reinício."),
    _spec("user_agents", Section.NETWORK, "User-Agents", FieldKind.LIST, FieldClass.HOT,
          "Lista de User-Agents usada em rotação nas requisições de scraping."),

    # ---- cache ----
    _spec("cache_backend", Section.CACHE, "Backend do cache", FieldKind.ENUM, FieldClass.COLD,
          "memory ou redis. Exige reinício.", choices=("memory", "redis")),
    _spec("cache_ttl_profile_seconds", Section.CACHE, "TTL de perfil (s)", FieldKind.INTEGER,
          FieldClass.HOT,
          "Quanto tempo o resultado de um perfil fica em cache. Mudar invalida as entradas dessa família."),
    _spec("cache_ttl_posts_seconds", Section.CACHE, "TTL de listagem de posts (s)",
          FieldKind.INTEGER, FieldClass.HOT,
          "Validade da listagem de posts de um perfil. Mudar invalida as entradas dessa família."),
    _spec("cache_ttl_post_seconds", Section.CACHE, "TTL de post (s)", FieldKind.INTEGER,
          FieldClass.HOT,
          "Validade do resultado de um post individual. Mudar invalida as entradas dessa família."),
    _spec("cache_ttl_blocked_seconds", Section.CACHE, "TTL de bloqueio (s)", FieldKind.INTEGER,
          FieldClass.HOT,
          "Validade curta para resultados bloqueados ou limitados, para o serviço se recuperar sozinho."),
    _spec("cache_max_entries", Section.CACHE, "Entradas máximas", FieldKind.INTEGER,
          FieldClass.COLD, "Teto de itens no cache em memória. Exige reinício."),
    _spec("redis_url", Section.CACHE, "URL do Redis", FieldKind.STRING, FieldClass.COLD,
          "Endereço do Redis quando o backend é redis. Exige reinício."),

    # ---- limites por plataforma ----
    _spec("ratelimit_youtube_per_minute", Section.RATELIMIT, "YouTube por minuto",
          FieldKind.INTEGER, FieldClass.HOT,
          "Requisições ao YouTube por minuto, somadas entre todos os clientes."),
    _spec("ratelimit_tiktok_per_minute", Section.RATELIMIT, "TikTok por minuto",
          FieldKind.INTEGER, FieldClass.HOT,
          "Requisições ao TikTok por minuto, somadas entre todos os clientes."),
    _spec("ratelimit_instagram_per_minute", Section.RATELIMIT, "Instagram por minuto",
          FieldKind.INTEGER, FieldClass.HOT,
          "Requisições ao Instagram por minuto, somadas entre todos os clientes."),
    _spec("ratelimit_facebook_per_minute", Section.RATELIMIT, "Facebook por minuto",
          FieldKind.INTEGER, FieldClass.HOT,
          "Requisições ao Facebook por minuto, somadas entre todos os clientes."),

    # ---- concorrência ----
    _spec("concurrency_youtube", Section.CONCURRENCY, "YouTube simultâneo", FieldKind.INTEGER,
          FieldClass.HOT, "Coleta simultânea no YouTube."),
    _spec("concurrency_tiktok", Section.CONCURRENCY, "TikTok simultâneo", FieldKind.INTEGER,
          FieldClass.HOT, "Coleta simultânea no TikTok."),
    _spec("concurrency_instagram", Section.CONCURRENCY, "Instagram simultâneo",
          FieldKind.INTEGER, FieldClass.HOT, "Coleta simultânea no Instagram."),
    _spec("concurrency_facebook", Section.CONCURRENCY, "Facebook simultâneo", FieldKind.INTEGER,
          FieldClass.HOT, "Coleta simultânea no Facebook."),

    # ---- circuit breaker ----
    _spec("breaker_enabled", Section.BREAKER, "Breaker ligado", FieldKind.BOOLEAN,
          FieldClass.HOT, "Interrompe a plataforma que falha repetidamente."),
    _spec("breaker_failure_threshold", Section.BREAKER, "Falhas para abrir",
          FieldKind.INTEGER, FieldClass.HOT,
          "Quantas falhas seguidas abrem o breaker de uma plataforma."),
    _spec("breaker_cooldown_seconds", Section.BREAKER, "Espera do breaker (s)",
          FieldKind.INTEGER, FieldClass.HOT,
          "Tempo com o breaker aberto antes de tentar de novo."),

    # ---- banco de dados ----
    _spec("database_url", Section.DATABASE, "URL do banco", FieldKind.STRING, FieldClass.COLD,
          "Endereço SQLite usado pelo histórico e pela configuração. Exige reinício."),
    _spec("enable_history", Section.DATABASE, "Histórico ligado", FieldKind.BOOLEAN,
          FieldClass.COLD,
          "Liga a gravação e a leitura do histórico de métricas. Exige reinício."),

    # ---- APIs oficiais ----
    _spec("youtube_api_key", Section.OFFICIAL_APIS, "Chave da API do YouTube",
          FieldKind.SECRET, FieldClass.HOT,
          "Chave da API oficial do YouTube; sem ela o perfil é lido por HTML.",
          sensitive=True),
    _spec("instagram_app_id", Section.OFFICIAL_APIS, "App ID do Instagram",
          FieldKind.STRING, FieldClass.HOT,
          "Identificador público do app enviado no cabeçalho do Instagram."),

    # ---- cookies ----
    _spec("cookies_storage_backend", Section.COOKIES, "Origem dos cookies",
          FieldKind.ENUM, FieldClass.COLD,
          "local lê os arquivos do disco; s3 baixa de um bucket compatível. Exige reinício.",
          choices=("local", "s3")),
    _spec("s3_endpoint_url", Section.COOKIES, "Endpoint S3", FieldKind.STRING,
          FieldClass.COLD, "Endpoint do storage compatível com S3. Exige reinício."),
    _spec("s3_cookies_bucket", Section.COOKIES, "Bucket dos cookies", FieldKind.STRING,
          FieldClass.COLD, "Bucket onde os arquivos de cookie ficam. Exige reinício."),
    _spec("s3_cookies_prefix", Section.COOKIES, "Prefixo no bucket", FieldKind.STRING,
          FieldClass.COLD, "Prefixo das chaves dos cookies no bucket. Exige reinício."),
    _spec("s3_region_name", Section.COOKIES, "Região S3", FieldKind.STRING, FieldClass.COLD,
          "Região usada na assinatura das requisições. Exige reinício."),
    _spec("s3_access_key_id", Section.COOKIES, "Access key do S3", FieldKind.SECRET,
          FieldClass.COLD, "Chave de acesso do storage. Fica cifrada no banco.",
          sensitive=True),
    _spec("s3_secret_access_key", Section.COOKIES, "Secret key do S3", FieldKind.SECRET,
          FieldClass.COLD, "Segredo do storage. Fica cifrado no banco.", sensitive=True),
    _spec("s3_session_token", Section.COOKIES, "Token de sessão do S3", FieldKind.SECRET,
          FieldClass.COLD, "Token temporário do storage. Fica cifrado no banco.",
          sensitive=True),
    _spec("cookies_dir", Section.COOKIES, "Diretório dos cookies", FieldKind.PATH,
          FieldClass.COLD, "Diretório com um arquivo <plataforma>.txt por plataforma."),
    _spec("cookies_youtube_file", Section.COOKIES, "Arquivo de cookie do YouTube",
          FieldKind.PATH, FieldClass.COLD,
          "Caminho explícito do arquivo de cookies do YouTube. Exige reinício."),
    _spec("cookies_tiktok_file", Section.COOKIES, "Arquivo de cookie do TikTok",
          FieldKind.PATH, FieldClass.COLD,
          "Caminho explícito do arquivo de cookies do TikTok. Exige reinício."),
    _spec("cookies_instagram_file", Section.COOKIES, "Arquivo de cookie do Instagram",
          FieldKind.PATH, FieldClass.COLD,
          "Caminho explícito do arquivo de cookies do Instagram. Exige reinício."),
    _spec("cookies_facebook_file", Section.COOKIES, "Arquivo de cookie do Facebook",
          FieldKind.PATH, FieldClass.COLD,
          "Caminho explícito do arquivo de cookies do Facebook. Exige reinício."),

    # ---- coleta ----
    _spec("scrape_max_urls", Section.SCRAPING, "URLs por pedido", FieldKind.INTEGER,
          FieldClass.HOT, "Quantas URLs um pedido de scraping aceita de uma vez."),
    _spec("scrape_default_posts", Section.SCRAPING, "Posts por padrão", FieldKind.INTEGER,
          FieldClass.HOT, "Quantos posts a listagem devolve quando o pedido não diz."),
    _spec("enable_ytdlp_fallback", Section.SCRAPING, "Fallback yt-dlp", FieldKind.BOOLEAN,
          FieldClass.HOT, "Usa yt-dlp quando o HTML não traz as métricas."),

    # ---- autenticação e segredos ----
    _spec("api_auth_enabled", Section.SECURITY, "Exigir chave de API", FieldKind.BOOLEAN,
          FieldClass.ENV_ONLY,
          "Só o ambiente liga ou desliga a autenticação de /v1. Exige reinício."),
    _spec("mcp_enabled", Section.SECURITY, "Servidor MCP ligado", FieldKind.BOOLEAN,
          FieldClass.ENV_ONLY,
          "Só o ambiente publica o endpoint MCP. Exige reinício."),
    _spec("app_encryption_key", Section.SECURITY, "Chave de criptografia",
          FieldKind.SECRET, FieldClass.ENV_ONLY,
          "Chave que cifra os segredos guardados. Só o ambiente a fornece; trocá-la "
          "inutiliza o que já está cifrado.",
          sensitive=True),
    _spec("admin_username", Section.SECURITY, "Usuário do painel", FieldKind.STRING,
          FieldClass.ENV_ONLY,
          "Nome do operador criado no primeiro boot. O ambiente define o nome; a "
          "senha muda pela tela."),
    _spec("admin_password", Section.SECURITY, "Senha inicial", FieldKind.SECRET,
          FieldClass.ENV_ONLY,
          "Senha em claro usada uma única vez, no primeiro boot, para criar o "
          "operador. Depois disso a senha vive no banco e muda pela tela.",
          sensitive=True),
    _spec("admin_password_hash", Section.SECURITY, "Hash da senha inicial",
          FieldKind.SECRET, FieldClass.ENV_ONLY,
          "Alternativa ao texto em claro: um hash Argon2id, que vence a senha "
          "quando os dois estão definidos.",
          sensitive=True),
    _spec("admin_session_idle_seconds", Section.SECURITY, "Sessão ociosa (s)",
          FieldKind.INTEGER, FieldClass.COLD,
          "Tempo sem uso que encerra a sessão do painel. Exige reinício."),
    _spec("admin_session_absolute_seconds", Section.SECURITY, "Sessão máxima (s)",
          FieldKind.INTEGER, FieldClass.COLD,
          "Teto absoluto da sessão, mesmo com uso contínuo. Exige reinício."),
    _spec("admin_login_max_failures", Section.SECURITY, "Falhas até o bloqueio",
          FieldKind.INTEGER, FieldClass.COLD,
          "Tentativas de login erradas, por IP e usuário, antes do bloqueio. "
          "Exige reinício."),
    _spec("admin_login_window_seconds", Section.SECURITY, "Janela das falhas (s)",
          FieldKind.INTEGER, FieldClass.COLD,
          "Intervalo em que as falhas são contadas. Exige reinício."),
    _spec("admin_lockout_seconds", Section.SECURITY, "Duração do bloqueio (s)",
          FieldKind.INTEGER, FieldClass.COLD,
          "Tempo de bloqueio depois de atingir o limite de falhas. Exige reinício."),
)

#: Registry keyed by the environment name of each field.
FIELDS_BY_NAME: dict[str, FieldSpec] = {spec.name: spec for spec in FIELDS}

#: Registry keyed by the ``Settings`` attribute name.
FIELDS_BY_ATTRIBUTE: dict[str, FieldSpec] = {spec.attribute: spec for spec in FIELDS}


def settings_field_names() -> set[str]:
    """Environment names of every field declared on :class:`Settings`."""
    return {name.upper() for name in Settings.model_fields}


def spec_for(name: str) -> FieldSpec:
    """Look a field up by environment name (``LOG_LEVEL``) or attribute (``log_level``)."""
    spec = FIELDS_BY_NAME.get(name.upper()) or FIELDS_BY_ATTRIBUTE.get(name)
    if spec is None:
        raise KeyError(f"no configuration metadata for field: {name}")
    return spec


def fields_by_section() -> dict[Section, list[FieldSpec]]:
    """Fields grouped by panel section, each section in declaration order."""
    grouped: dict[Section, list[FieldSpec]] = {}
    for spec in FIELDS:
        grouped.setdefault(spec.section, []).append(spec)
    return grouped


def sensitive_names() -> set[str]:
    """Names of the fields whose value must never be returned in clear."""
    return {spec.name for spec in FIELDS if spec.sensitive}
