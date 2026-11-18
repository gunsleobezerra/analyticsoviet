"""Typed contract for the persisted configuration layer.

Every field of ``Settings`` is described exactly once here: its section, when a
stored value takes effect, whether it holds a secret and which interval it
accepts. The administrative API and the panel form are generated from this
registry, so a new ``Settings`` field that nobody classified fails the
completeness test instead of becoming an uneditable field with no warning.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import ConfigDict, TypeAdapter, ValidationError

from core.config import Settings


class Mutability(str, Enum):
    """When a stored override is accepted, and when it takes effect."""

    hot = "hot"
    restart_required = "restart_required"
    env_only = "env_only"
    read_only = "read_only"


class ConfigSource(str, Enum):
    """Where the effective value came from."""

    default = "default"
    env = "env"
    db = "db"


class OverrideRejected(ValueError):
    """An override was refused: unknown key, closed field or invalid value."""

    def __init__(self, key: str, code: str, message: str) -> None:
        super().__init__(f"{key}: {message}")
        self.key = key
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One configurable field, as the API and the panel must see it."""

    key: str
    section: str
    mutability: Mutability
    secret: bool
    annotation: Any
    default: Any
    minimum: int | float | None = None
    maximum: int | float | None = None

    @property
    def editable(self) -> bool:
        """True when ``PATCH`` may store a value for this field."""
        return self.mutability in (Mutability.hot, Mutability.restart_required)

    @property
    def requires_restart(self) -> bool:
        """True when a stored value only applies to a new process."""
        return self.mutability in (Mutability.restart_required, Mutability.env_only)

    @property
    def value_type(self) -> str:
        """JSON-facing type name, used by the panel to pick an input."""
        return _type_name(self.annotation)


# Section and mutability per field. Complete on purpose: the completeness test
# asserts this table covers every field of Settings, so adding a field without
# classifying it fails the suite.
_CLASSIFICATION: dict[str, tuple[str, Mutability]] = {
    # app
    "APP_NAME": ("app", Mutability.read_only),
    "APP_VERSION": ("app", Mutability.read_only),
    "ENVIRONMENT": ("app", Mutability.env_only),
    "LOG_LEVEL": ("app", Mutability.hot),
    "API_PREFIX": ("app", Mutability.env_only),
    "PUBLIC_BASE_URL": ("app", Mutability.restart_required),
    # http
    "HTTP_TIMEOUT_SECONDS": ("http", Mutability.restart_required),
    "HTTP_CONNECT_TIMEOUT_SECONDS": ("http", Mutability.restart_required),
    "HTTP_MAX_CONNECTIONS": ("http", Mutability.restart_required),
    "HTTP_MAX_KEEPALIVE_CONNECTIONS": ("http", Mutability.restart_required),
    "HTTP_RETRIES": ("http", Mutability.hot),
    "HTTP_RETRY_BACKOFF_SECONDS": ("http", Mutability.hot),
    "PROXY_URL": ("http", Mutability.restart_required),
    "USER_AGENTS": ("http", Mutability.hot),
    # cache
    "CACHE_BACKEND": ("cache", Mutability.restart_required),
    "CACHE_TTL_PROFILE_SECONDS": ("cache", Mutability.hot),
    "CACHE_TTL_POSTS_SECONDS": ("cache", Mutability.hot),
    "CACHE_TTL_POST_SECONDS": ("cache", Mutability.hot),
    "CACHE_TTL_BLOCKED_SECONDS": ("cache", Mutability.hot),
    "CACHE_MAX_ENTRIES": ("cache", Mutability.hot),
    "REDIS_URL": ("cache", Mutability.restart_required),
    # rate limiting
    "RATELIMIT_YOUTUBE_PER_MINUTE": ("ratelimit", Mutability.hot),
    "RATELIMIT_TIKTOK_PER_MINUTE": ("ratelimit", Mutability.hot),
    "RATELIMIT_INSTAGRAM_PER_MINUTE": ("ratelimit", Mutability.hot),
    "RATELIMIT_FACEBOOK_PER_MINUTE": ("ratelimit", Mutability.hot),
    # concurrency
    "CONCURRENCY_YOUTUBE": ("concurrency", Mutability.hot),
    "CONCURRENCY_TIKTOK": ("concurrency", Mutability.hot),
    "CONCURRENCY_INSTAGRAM": ("concurrency", Mutability.hot),
    "CONCURRENCY_FACEBOOK": ("concurrency", Mutability.hot),
    # circuit breaker
    "BREAKER_ENABLED": ("breaker", Mutability.hot),
    "BREAKER_FAILURE_THRESHOLD": ("breaker", Mutability.hot),
    "BREAKER_COOLDOWN_SECONDS": ("breaker", Mutability.hot),
    # storage
    "DATABASE_URL": ("storage", Mutability.env_only),
    "ENABLE_HISTORY": ("storage", Mutability.restart_required),
    # optional official APIs
    "YOUTUBE_API_KEY": ("api", Mutability.hot),
    "INSTAGRAM_APP_ID": ("api", Mutability.hot),
    # cookies storage backend
    "COOKIES_STORAGE_BACKEND": ("cookies", Mutability.restart_required),
    "S3_ENDPOINT_URL": ("cookies", Mutability.hot),
    "S3_COOKIES_BUCKET": ("cookies", Mutability.hot),
    "S3_COOKIES_PREFIX": ("cookies", Mutability.hot),
    "S3_REGION_NAME": ("cookies", Mutability.hot),
    "S3_ACCESS_KEY_ID": ("cookies", Mutability.hot),
    "S3_SECRET_ACCESS_KEY": ("cookies", Mutability.hot),
    "S3_SESSION_TOKEN": ("cookies", Mutability.hot),
    # cookies files
    "COOKIES_DIR": ("cookies", Mutability.restart_required),
    "COOKIES_YOUTUBE_FILE": ("cookies", Mutability.restart_required),
    "COOKIES_TIKTOK_FILE": ("cookies", Mutability.restart_required),
    "COOKIES_INSTAGRAM_FILE": ("cookies", Mutability.restart_required),
    "COOKIES_FACEBOOK_FILE": ("cookies", Mutability.restart_required),
    # scraping limits
    "SCRAPE_MAX_URLS": ("scraping", Mutability.hot),
    "SCRAPE_DEFAULT_POSTS": ("scraping", Mutability.hot),
    # feature toggles
    "ENABLE_YTDLP_FALLBACK": ("scraping", Mutability.hot),
}

# Accepted interval per numeric field, inclusive. A value outside it is refused
# before it reaches the database, so a typo cannot stop the next boot.
_BOUNDS: dict[str, tuple[int | float | None, int | float | None]] = {
    "HTTP_TIMEOUT_SECONDS": (1, 300),
    "HTTP_CONNECT_TIMEOUT_SECONDS": (1, 300),
    "HTTP_MAX_CONNECTIONS": (1, 1000),
    "HTTP_MAX_KEEPALIVE_CONNECTIONS": (0, 1000),
    "HTTP_RETRIES": (0, 10),
    "HTTP_RETRY_BACKOFF_SECONDS": (0, 60),
    "CACHE_TTL_PROFILE_SECONDS": (0, 604800),
    "CACHE_TTL_POSTS_SECONDS": (0, 604800),
    "CACHE_TTL_POST_SECONDS": (0, 604800),
    "CACHE_TTL_BLOCKED_SECONDS": (0, 86400),
    "CACHE_MAX_ENTRIES": (1, 1000000),
    "RATELIMIT_YOUTUBE_PER_MINUTE": (1, 6000),
    "RATELIMIT_TIKTOK_PER_MINUTE": (1, 6000),
    "RATELIMIT_INSTAGRAM_PER_MINUTE": (1, 6000),
    "RATELIMIT_FACEBOOK_PER_MINUTE": (1, 6000),
    "CONCURRENCY_YOUTUBE": (1, 64),
    "CONCURRENCY_TIKTOK": (1, 64),
    "CONCURRENCY_INSTAGRAM": (1, 64),
    "CONCURRENCY_FACEBOOK": (1, 64),
    "BREAKER_FAILURE_THRESHOLD": (1, 100),
    "BREAKER_COOLDOWN_SECONDS": (0, 3600),
    "SCRAPE_MAX_URLS": (1, 200),
    "SCRAPE_DEFAULT_POSTS": (0, 100),
}

# Fields whose value must never leave the process. Stored encrypted, returned as
# a presence marker only, and masked in the audit trail.
SECRET_KEYS: frozenset[str] = frozenset(
    {
        "YOUTUBE_API_KEY",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        "S3_SESSION_TOKEN",
    }
)

SECRET_MARKER = "***"

_JSON_ADAPTERS: dict[str, TypeAdapter[Any]] = {}


def _type_name(annotation: Any) -> str:
    """Return the JSON-facing type name for a pydantic field annotation."""
    origin = get_origin(annotation)
    if origin is Literal:
        return "enum"
    if origin in (list, tuple, set, frozenset):
        return "list"
    if origin is Union or str(origin) == "types.UnionType" or type(annotation).__name__ == "UnionType":
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        return _type_name(args[0]) if len(args) == 1 else "string"
    if annotation is bool:
        return "boolean"
    if annotation is int:
        return "integer"
    if annotation is float:
        return "number"
    if annotation is str:
        return "string"
    if annotation is Path:
        return "path"
    return "string"


def _adapter(spec: FieldSpec) -> TypeAdapter[Any]:
    """Adapter for one field: strict, so ``"60"`` never becomes ``60``.

    Path fields are the exception: the wire format is a string, and only a lax
    adapter turns it into ``Path``.
    """
    cached = _JSON_ADAPTERS.get(spec.key)
    if cached is None:
        config = None if spec.value_type == "path" else ConfigDict(strict=True)
        cached = TypeAdapter(spec.annotation, config=config)
        _JSON_ADAPTERS[spec.key] = cached
    return cached


def _build() -> dict[str, FieldSpec]:
    specs: dict[str, FieldSpec] = {}
    for name, field in Settings.model_fields.items():
        key = name.upper()
        section, mutability = _CLASSIFICATION.get(key, ("misc", Mutability.hot))
        minimum, maximum = _BOUNDS.get(key, (None, None))
        specs[key] = FieldSpec(
            key=key,
            section=section,
            mutability=mutability,
            secret=key in SECRET_KEYS,
            annotation=field.annotation,
            default=field.get_default(call_default_factory=True),
            minimum=minimum,
            maximum=maximum,
        )
    return specs


FIELDS: dict[str, FieldSpec] = _build()


def iter_fields() -> Iterator[FieldSpec]:
    """Every field, in declaration order."""
    return iter(FIELDS.values())


def sections() -> dict[str, list[FieldSpec]]:
    """Fields grouped by section, sections in first-declaration order."""
    grouped: dict[str, list[FieldSpec]] = {}
    for spec in FIELDS.values():
        grouped.setdefault(spec.section, []).append(spec)
    return grouped


def get_field(key: str) -> FieldSpec:
    """Look up one field, refusing keys that ``Settings`` does not declare."""
    spec = FIELDS.get(key.upper())
    if spec is None:
        raise OverrideRejected(key, "unknown_key", "campo não existe em Settings")
    return spec


def coerce_override(key: str, raw: Any) -> Any:
    """Validate a JSON value against the field contract and return it typed.

    Raises ``OverrideRejected`` when the field is closed for overrides, when the
    value has the wrong type, or when it falls outside the accepted interval.
    """
    spec = get_field(key)
    if spec.mutability is Mutability.env_only:
        raise OverrideRejected(key, "env_only", "campo só aceita valor pelo ambiente")
    if spec.mutability is Mutability.read_only:
        raise OverrideRejected(key, "read_only", "campo é somente leitura")
    if spec.secret and not isinstance(raw, str):
        raise OverrideRejected(key, "invalid_value", "segredo deve ser texto")
    try:
        value = _adapter(spec).validate_python(raw)
    except ValidationError as exc:
        raise OverrideRejected(key, "invalid_value", exc.errors()[0]["msg"]) from exc
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        if spec.minimum is not None and value < spec.minimum:
            raise OverrideRejected(key, "invalid_value", f"mínimo {spec.minimum}")
        if spec.maximum is not None and value > spec.maximum:
            raise OverrideRejected(key, "invalid_value", f"máximo {spec.maximum}")
    return value


def dump_override(key: str, raw: Any) -> str:
    """Validate then serialize a value the way ``config_overrides.value_json`` stores it."""
    value = coerce_override(key, raw)
    if isinstance(value, Path):
        value = str(value)
    elif isinstance(value, list | tuple):
        value = [str(item) if isinstance(item, Path) else item for item in value]
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load_override(key: str, stored: str) -> Any:
    """Parse a stored ``value_json`` back into the field's typed value."""
    return coerce_override(key, json.loads(stored))


def mask(key: str, value: Any, is_set: bool) -> Any:
    """What the API may show for a field: never a secret value, never a hash."""
    spec = get_field(key)
    if spec.secret:
        return SECRET_MARKER if is_set else None
    if isinstance(value, Path):
        return str(value)
    return value
