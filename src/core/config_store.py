"""Contract and database-backed implementation of the configuration layer.

This module is the seam between the stored configuration and the code that reads
it. It declares the types and the interface, and at the bottom it implements
them against the override and secret tables.

Precedence, from weakest to strongest: the field default, then the environment
(``.env`` included), then the database override. ``snapshot()`` resolves all
three and returns a validated :class:`EffectiveSettings`; ``current()`` returns
the snapshot already in memory, which is what a request handler calls.

Callers depend on the contract, not on the storage: the admin API and the panel
never see a session, a row or a ciphertext.
"""

from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import TypeAdapter, ValidationError

from core.config import Settings
from core.config_crypto import CONFIG_SECRET_PURPOSE, ConfigCryptoError, SealedSecret, SecretCipher
from core.config_fields import FIELDS, FIELDS_BY_ATTRIBUTE, FIELDS_BY_NAME, FieldClass, FieldKind, FieldSpec, Section
from core.logging import get_logger

LOGGER = get_logger("config")


class ValueOrigin(StrEnum):
    """Where the value in force came from."""

    DEFAULT = "default"
    ENV = "env"
    DATABASE = "database"
    VAULT = "vault"


class CacheFamily(StrEnum):
    """The TTL families whose cache keys carry an epoch.

    Lowering a TTL cannot shorten an entry that is already stored, because the
    expiry lives inside the entry. Bumping the epoch of a family changes every
    key of that family, which invalidates the old entries without scanning.
    """

    PROFILE = "profile"
    POSTS = "posts"
    POST = "post"
    BLOCKED = "blocked"


#: TTL field that owns each cache family. Changing the field bumps its family.
TTL_ATTRIBUTES: Mapping[CacheFamily, str] = {
    CacheFamily.PROFILE: "cache_ttl_profile_seconds",
    CacheFamily.POSTS: "cache_ttl_posts_seconds",
    CacheFamily.POST: "cache_ttl_post_seconds",
    CacheFamily.BLOCKED: "cache_ttl_blocked_seconds",
}


@dataclass(frozen=True)
class FieldValue:
    """One field as the panel sees it.

    ``value`` is ``None`` for a secret: the panel learns whether a secret is set,
    never what it is.
    """

    spec: FieldSpec
    value: Any
    origin: ValueOrigin
    is_set: bool
    pending_restart: bool = False

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def is_secret(self) -> bool:
        return self.spec.sensitive

    @property
    def editable(self) -> bool:
        """Whether the panel may offer an input for this field."""
        return self.spec.writable


@dataclass(frozen=True)
class EffectiveSettings:
    """The settings in force, with the provenance of each field.

    Attribute access delegates to the underlying :class:`Settings`, so callers
    read ``effective.log_level`` exactly as they read ``settings.log_level``. The
    object is built once per snapshot and never mutated: a write produces a new
    snapshot, which is what makes a request see one consistent configuration.
    """

    settings: Settings = field(repr=False)
    origins: Mapping[str, ValueOrigin]
    version: int
    loaded_at: datetime

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes this dataclass does not define.
        try:
            return getattr(self.settings, name)
        except AttributeError as exc:  # pragma: no cover - typo in caller
            raise AttributeError(f"no such setting: {name}") from exc

    def origin_of(self, name: str) -> ValueOrigin:
        """Provenance of one field, by environment name (``LOG_LEVEL``)."""
        return self.origins.get(name.upper(), ValueOrigin.DEFAULT)

    def value_of(self, name: str) -> Any:
        """Value in force for one field, by environment name."""
        return getattr(self.settings, name.lower())

    def is_set(self, name: str) -> bool:
        """Whether the field has a value beyond its default."""
        return self.origin_of(name) is not ValueOrigin.DEFAULT

    def pending_restart(self) -> tuple[str, ...]:
        """Fields changed in the database that only a new process will pick up."""
        return tuple(
            name
            for name, origin in self.origins.items()
            if origin is ValueOrigin.DATABASE and _spec_for(name).requires_restart
        )

    def as_public_dict(self) -> dict[str, Any]:
        """Flat ``{FIELD_NAME: value}`` view with secrets withheld.

        Useful for the health/status payload and for logging: a secret never
        appears here, only ``None`` — ``views()`` carries the ``is_set`` flag
        that tells the panel whether a secret exists.
        """
        return {
            spec.name: (
                None if spec.sensitive else getattr(self.settings, spec.attribute, None)
            )
            for spec in FIELDS
        }

    def views(self) -> tuple[FieldValue, ...]:
        """One :class:`FieldValue` per field, in registry order."""
        result: list[FieldValue] = []
        for spec in FIELDS:
            origin = self.origin_of(spec.name)
            raw = getattr(self.settings, spec.attribute, None)
            is_set = origin is not ValueOrigin.DEFAULT
            result.append(
                FieldValue(
                    spec=spec,
                    value=None if spec.sensitive else raw,
                    origin=origin,
                    is_set=is_set,
                    pending_restart=is_set and spec.requires_restart,
                )
            )
        return tuple(result)


def _spec_for(name: str) -> FieldSpec:
    from core.config_fields import spec_for

    return spec_for(name)


class ConfigStore(ABC):
    """Resolves, caches and invalidates the configuration in force.

    Implementations must satisfy three properties, because the rest of the
    application relies on them:

    * **One snapshot per request.** ``current()`` returns the same object for the
      whole request, so a handler never sees two versions of a value.
    * **Writes are visible on the next read.** After a write, ``version`` grows
      and ``invalidate()`` drops the local snapshot; other workers notice through
      the version counter.
    * **Secrets never leave the store in clear.** A secret field is exposed as
      "set or not", never as a value.
    """

    @abstractmethod
    async def snapshot(self) -> EffectiveSettings:
        """Resolve the configuration and return a validated snapshot.

        Reads the overrides and the version counter, merges them over the
        environment, validates the result through :class:`Settings` and caches
        it. When the database is unreachable the implementation decides between
        serving the last good snapshot and failing — that policy belongs to the
        store, not to this contract.
        """

    @abstractmethod
    def current(self) -> EffectiveSettings:
        """Return the snapshot already in memory.

        This is the request-path accessor: no I/O, no validation, no awaits. It
        raises ``RuntimeError`` when no snapshot has been loaded yet.
        """

    @property
    @abstractmethod
    def version(self) -> int:
        """Configuration version in force; grows with every committed write."""

    @abstractmethod
    def cache_epoch(self, family: CacheFamily) -> int:
        """Current epoch of one TTL family, used as part of the cache key.

        Bumping the epoch is how a TTL change invalidates entries that are
        already stored without walking the cache.
        """

    @abstractmethod
    async def invalidate(self) -> None:
        """Drop the in-memory snapshot so the next read resolves it again.

        Called by the writer after a commit, in every worker that can serve a
        request; it does not touch the database.
        """


# --------------------------------------------------------------------------- #
# stored shape and storage seam
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StoredSecret:
    """One encrypted secret exactly as it comes back from storage."""

    ciphertext: bytes
    nonce: bytes
    key_version: int
    purpose: str


@dataclass(frozen=True)
class StoredConfig:
    """Everything the resolution needs, read in one go."""

    version: int
    overrides: Mapping[str, str]
    secrets: Mapping[str, StoredSecret]


@dataclass(frozen=True)
class AuditEntry:
    """One line of the audit trail, before it reaches the database."""

    actor_user_id: str
    actor_username: str
    action: str
    config_key: str
    old_value: str | None
    new_value: str | None
    is_secret: bool
    source: str
    applied_mode: str
    ip: str | None = None
    request_id: str = ""


class ConfigSource(Protocol):
    """Storage the store needs. The SQL implementation lives in ``db``."""

    async def load(self) -> StoredConfig: ...

    async def set_override(self, key: str, value_json: str, *, entry: AuditEntry) -> int: ...

    async def set_secret(self, key: str, sealed: SealedSecret, *, entry: AuditEntry) -> int: ...

    async def reset(self, key: str, *, entry: AuditEntry) -> int: ...


class ConfigWriteError(ValueError):
    """A write was refused before it reached the database.

    ``code`` is what the admin API turns into an error payload: ``env_only`` and
    ``read_only`` are refusals by classification, ``invalid_value`` by
    validation, ``secret_in_overrides`` and ``encryption_key_missing`` by the
    secret rules.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConfigDataError(RuntimeError):
    """Stored configuration cannot be turned into valid settings."""


# --------------------------------------------------------------------------- #
# database-backed implementation
# --------------------------------------------------------------------------- #


class PersistentConfigStore(ConfigStore):
    """Resolves default < environment < database, and keeps the result warm.

    The version counter is the only thing shared between workers: a worker that
    sees a new version rebuilds its snapshot, so two workers cannot serve
    different values for longer than one reload interval. The snapshot in memory
    is what a request reads, and it is replaced, never mutated, so a request that
    started under one version finishes under that version.

    Without a reachable database the store serves the environment and the
    defaults and records why, instead of failing the boot. The exception is a
    stored secret that cannot be opened: that fails closed, because serving a
    configuration with a broken credential is worse than not serving.
    """

    def __init__(
        self,
        settings: Settings,
        source: ConfigSource,
        cipher: SecretCipher | None = None,
        *,
        reload_interval_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base = settings
        self._source = source
        self._cipher = cipher
        self._reload_interval = max(reload_interval_seconds, 0.0)
        self._clock = clock
        self._snapshot: EffectiveSettings | None = None
        self._epochs: dict[CacheFamily, int] = dict.fromkeys(CacheFamily, 0)
        self._dirty = True
        self._last_check = float("-inf")
        self.degraded_reason: str | None = None

    # -- reads ------------------------------------------------------------- #

    async def snapshot(self) -> EffectiveSettings:
        now = self._clock()
        if not self._dirty and self._snapshot is not None and now - self._last_check < self._reload_interval:
            return self._snapshot

        try:
            stored = await self._source.load()
        except Exception as exc:  # any storage failure degrades the store, never the boot
            return self._degrade(exc)

        self._last_check = now
        if not self._dirty and self._snapshot is not None and stored.version == self._snapshot.version:
            # The source answered again, so whatever made it degrade is gone:
            # a status reader must not keep seeing an old failure.
            self.degraded_reason = None
            return self._snapshot

        previous = self._snapshot
        resolved = self._resolve(stored)
        self._bump_epochs(previous, resolved)
        self._snapshot = resolved
        self._dirty = False
        self.degraded_reason = None
        return resolved

    def current(self) -> EffectiveSettings:
        if self._snapshot is None:
            raise RuntimeError("configuration not loaded yet; await snapshot() first")
        return self._snapshot

    @property
    def version(self) -> int:
        return self._snapshot.version if self._snapshot is not None else 0

    def cache_epoch(self, family: CacheFamily) -> int:
        return self._epochs[CacheFamily(family)]

    async def invalidate(self) -> None:
        self._dirty = True
        self._last_check = float("-inf")

    # -- writes ------------------------------------------------------------ #

    async def set_override(
        self,
        name: str,
        raw: Any,
        *,
        actor_user_id: str,
        actor_username: str,
        ip: str | None = None,
        request_id: str = "",
        source: str = "cli",
    ) -> EffectiveSettings:
        """Store a non-secret value and return the configuration it produces."""
        spec = _require_writable(name, secret=False)
        value = _coerce(spec, raw)
        encoded = json.dumps(value, default=str)
        entry = AuditEntry(
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            action="set",
            config_key=spec.name,
            old_value=_audit_text(self._current_value(spec)),
            new_value=_audit_text(value),
            is_secret=False,
            source=source,
            applied_mode=_applied_mode(spec),
            ip=ip,
            request_id=request_id,
        )
        await self._source.set_override(spec.name, encoded, entry=entry)
        return await self._reload()

    async def set_secret(
        self,
        name: str,
        plaintext: str,
        *,
        actor_user_id: str,
        actor_username: str,
        ip: str | None = None,
        request_id: str = "",
        source: str = "cli",
    ) -> EffectiveSettings:
        """Seal a secret and store only the ciphertext.

        Without a key the write is refused; falling back to plaintext would
        quietly downgrade every guarantee the table exists to provide.
        """
        spec = _require_writable(name, secret=True)
        if self._cipher is None:
            raise ConfigWriteError(
                "encryption_key_missing",
                "APP_ENCRYPTION_KEY is not set, so a secret cannot be stored",
            )
        was_set = self._current_value(spec) is not None
        sealed = self._cipher.seal(plaintext, purpose=CONFIG_SECRET_PURPOSE, row_id=spec.name)
        entry = AuditEntry(
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            action="set",
            config_key=spec.name,
            old_value="***" if was_set else None,
            new_value="***",
            is_secret=True,
            source=source,
            applied_mode=_applied_mode(spec),
            ip=ip,
            request_id=request_id,
        )
        await self._source.set_secret(spec.name, sealed, entry=entry)
        return await self._reload()

    async def reset(
        self,
        name: str,
        *,
        actor_user_id: str,
        actor_username: str,
        ip: str | None = None,
        request_id: str = "",
        source: str = "cli",
    ) -> EffectiveSettings:
        """Drop a stored value, returning the field to the environment layer."""
        spec = _require_writable(name, secret=None)
        current = self._current_value(spec)
        entry = AuditEntry(
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            action="reset",
            config_key=spec.name,
            old_value=None if current is None else _audit_text(current),
            new_value=None,
            is_secret=spec.sensitive,
            source=source,
            applied_mode=_applied_mode(spec),
            ip=ip,
            request_id=request_id,
        )
        await self._source.reset(spec.name, entry=entry)
        return await self._reload()

    # -- internals --------------------------------------------------------- #

    async def _reload(self) -> EffectiveSettings:
        await self.invalidate()
        return await self.snapshot()

    def _resolve(self, stored: StoredConfig) -> EffectiveSettings:
        values: dict[str, Any] = {}
        origins = self._environment_origins()

        for name, raw in stored.overrides.items():
            spec = FIELDS_BY_NAME.get(name)
            if spec is None or not spec.writable:
                # A row for a field only the environment may set must never take
                # effect, not even when it was written by hand or by an older build.
                continue
            try:
                values[spec.attribute] = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ConfigDataError(f"stored value for {name} is not valid JSON") from exc
            origins[name] = ValueOrigin.DATABASE

        for name, secret in stored.secrets.items():
            spec = FIELDS_BY_NAME.get(name)
            if spec is None or not spec.writable:
                continue
            values[spec.attribute] = self._open_secret(spec, secret)
            origins[name] = ValueOrigin.DATABASE

        merged = {**self._base.model_dump(), **values}
        try:
            settings = Settings(**merged)
        except ValidationError as exc:
            raise ConfigDataError(f"stored configuration is invalid: {_invalid_fields(exc)}") from exc

        return EffectiveSettings(
            settings=settings,
            origins=origins,
            version=stored.version,
            loaded_at=datetime.now(UTC),
        )

    def _open_secret(self, spec: FieldSpec, secret: StoredSecret) -> str:
        if self._cipher is None:
            raise ConfigCryptoError(
                f"{spec.name} is stored encrypted but APP_ENCRYPTION_KEY is not set"
            )
        sealed = SealedSecret(
            ciphertext=secret.ciphertext,
            nonce=secret.nonce,
            key_version=secret.key_version,
            purpose=secret.purpose,
        )
        return self._cipher.open(sealed, purpose=secret.purpose, row_id=spec.name)

    def _environment_origins(self) -> dict[str, ValueOrigin]:
        provided = {name.upper() for name in self._base.model_fields_set}
        return {
            spec.name: ValueOrigin.ENV if spec.name in provided else ValueOrigin.DEFAULT
            for spec in FIELDS
        }

    def _bump_epochs(self, previous: EffectiveSettings | None, current: EffectiveSettings) -> None:
        """Invalidate the families whose TTL changed since the previous snapshot.

        Each worker counts the bumps it has seen, so two workers that observe the
        same sequence of snapshots agree on the epoch. They can drift when one of
        them skips a version: a TTL that changes and changes back between two
        reads leaves that worker one bump ahead. The effect is a different cache
        namespace per worker until it restarts, which costs duplicate fetches and
        never serves a wrong value, because the entries of the old epoch are
        orphaned rather than reused. Versioning the epoch itself, so a skipped
        bump cannot separate the workers, is a later step.
        """
        if previous is None:
            return
        for family, attribute in TTL_ATTRIBUTES.items():
            if getattr(previous.settings, attribute) != getattr(current.settings, attribute):
                self._epochs[family] += 1

    def _degrade(self, exc: Exception) -> EffectiveSettings:
        """Serve what is available and record why, instead of failing the boot."""
        self._last_check = self._clock()
        self.degraded_reason = sanitize_failure(exc)
        if self._snapshot is not None:
            LOGGER.warning("config_degraded", reason=self.degraded_reason, served="last snapshot")
            return self._snapshot

        resolved = self._resolve(StoredConfig(version=0, overrides={}, secrets={}))
        self._snapshot = resolved
        self._dirty = False
        LOGGER.warning("config_degraded", reason=self.degraded_reason, served="environment and defaults")
        return resolved

    def _current_value(self, spec: FieldSpec) -> Any:
        if self._snapshot is None:
            return None
        return getattr(self._snapshot.settings, spec.attribute, None)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

#: Credentials embedded in a connection URL, and parameters that carry a secret.
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<credentials>[^/\s@]+)@")
_SENSITIVE_PARAMETER = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api_?key|access_key|private_key)=([^&\s,;]+)"
)


def sanitize_failure(exc: BaseException) -> str:
    """Describe a failure without carrying a credential into a log or a status.

    A database driver quotes the connection string it failed on, and that string
    may hold a user and a password. What a caller sees keeps the host and the
    parameter names, and loses the values that must never be written down.
    """
    message = f"{type(exc).__name__}: {exc}"
    message = _URL_CREDENTIALS.sub(lambda match: f"{match.group('scheme')}***@", message)
    return _SENSITIVE_PARAMETER.sub(lambda match: f"{match.group(1)}=***", message)


def _require_writable(name: str, *, secret: bool | None) -> FieldSpec:
    spec = FIELDS_BY_NAME.get(name.upper()) or FIELDS_BY_ATTRIBUTE.get(name)
    if spec is None:
        raise ConfigWriteError("unknown_field", f"no such configuration field: {name}")
    if spec.cls is FieldClass.ENV_ONLY:
        raise ConfigWriteError("env_only", f"{spec.name} can only be set through the environment")
    if spec.cls is FieldClass.READ_ONLY:
        raise ConfigWriteError("read_only", f"{spec.name} is read-only")
    if secret is True and not spec.sensitive:
        raise ConfigWriteError("not_a_secret", f"{spec.name} is not a secret; store it as an override")
    if secret is False and spec.sensitive:
        raise ConfigWriteError(
            "secret_in_overrides", f"{spec.name} is a secret; it cannot be stored in clear"
        )
    return spec


def _coerce(spec: FieldSpec, raw: Any) -> Any:
    """Validate one value against the annotation declared on ``Settings``."""
    annotation = Settings.model_fields[spec.attribute].annotation
    try:
        return TypeAdapter(annotation).validate_python(raw)
    except ValidationError as exc:
        reason = exc.errors()[0].get("msg", "invalid value")
        raise ConfigWriteError("invalid_value", f"{spec.name}: {reason}") from exc


def _applied_mode(spec: FieldSpec) -> str:
    return "restart_required" if spec.requires_restart else "hot"


def _audit_text(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value, default=str)


def _invalid_fields(exc: ValidationError) -> str:
    return ", ".join(".".join(str(part) for part in error["loc"]) for error in exc.errors())


__all__ = [
    "TTL_ATTRIBUTES",
    "AuditEntry",
    "CacheFamily",
    "ConfigDataError",
    "ConfigSource",
    "ConfigStore",
    "ConfigWriteError",
    "EffectiveSettings",
    "FieldClass",
    "FieldKind",
    "FieldSpec",
    "FieldValue",
    "PersistentConfigStore",
    "Section",
    "StoredConfig",
    "StoredSecret",
    "ValueOrigin",
    "sanitize_failure",
]
