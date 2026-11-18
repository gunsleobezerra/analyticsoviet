"""Contract for the configuration layer.

This module is the seam between the stored configuration and the code that reads
it. It declares the types and the interface; the resolution itself (database
overrides on top of the environment, the version counter, the cache epochs) is
implemented by the store that fills this contract in.

Precedence, from weakest to strongest: the field default, then the environment
(``.env`` included), then the database override. ``snapshot()`` resolves all
three and returns a validated :class:`EffectiveSettings`; ``current()`` returns
the snapshot already in memory, which is what a request handler calls.

Nothing here talks to a database on purpose: the callers of this contract are the
admin API and the panel, and they should not depend on how the values are stored.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from core.config import Settings
from core.config_fields import FieldClass, FieldKind, FieldSpec, Section


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
        from core.config_fields import FIELDS

        return {
            spec.name: (
                None if spec.sensitive else getattr(self.settings, spec.attribute, None)
            )
            for spec in FIELDS
        }

    def views(self) -> tuple[FieldValue, ...]:
        """One :class:`FieldValue` per field, in registry order."""
        from core.config_fields import FIELDS

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


__all__ = [
    "CacheFamily",
    "ConfigStore",
    "EffectiveSettings",
    "FieldClass",
    "FieldKind",
    "FieldSpec",
    "FieldValue",
    "Section",
    "ValueOrigin",
]
