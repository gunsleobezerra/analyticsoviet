"""SQL storage for the persisted configuration.

One place knows how overrides, secrets and the version counter are laid out. A
write is one transaction: the value, its audit row and the version bump commit
together, so a reader never sees a value without the version that announces it
and never sees a version without the value it counts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.config_crypto import SealedSecret
from core.config_store import AuditEntry, StoredConfig, StoredSecret
from db import audit
from db.models import ConfigOverride, ConfigSecret, ConfigState
from db.session import Database


class ConfigRepository:
    """Reads and writes the configuration tables over one async engine."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def load(self) -> StoredConfig:
        """Read the version, every override and every sealed secret."""
        async with self._database.session() as session:
            version = await session.scalar(select(ConfigState.version).where(ConfigState.id == 1))
            overrides = (await session.execute(select(ConfigOverride))).scalars().all()
            secrets = (await session.execute(select(ConfigSecret))).scalars().all()

        return StoredConfig(
            version=int(version or 1),
            overrides={row.config_key: row.value_json for row in overrides},
            secrets={
                row.config_key: StoredSecret(
                    ciphertext=row.ciphertext,
                    nonce=row.nonce,
                    key_version=row.key_version,
                    purpose=row.aad,
                )
                for row in secrets
            },
        )

    async def set_override(self, key: str, value_json: str, *, entry: AuditEntry) -> int:
        async def apply(session: AsyncSession) -> None:
            row = await session.get(ConfigOverride, key)
            if row is None:
                session.add(
                    ConfigOverride(
                        config_key=key,
                        value_json=value_json,
                        updated_at=audit.utc_stamp(),
                        updated_by=entry.actor_user_id,
                        updated_ip=entry.ip,
                    )
                )
                return
            row.value_json = value_json
            row.updated_at = audit.utc_stamp()
            row.updated_by = entry.actor_user_id
            row.updated_ip = entry.ip

        return await self._commit(apply, entry)

    async def set_secret(self, key: str, sealed: SealedSecret, *, entry: AuditEntry) -> int:
        async def apply(session: AsyncSession) -> None:
            row = await session.get(ConfigSecret, key)
            if row is None:
                session.add(
                    ConfigSecret(
                        config_key=key,
                        ciphertext=sealed.ciphertext,
                        nonce=sealed.nonce,
                        key_version=sealed.key_version,
                        aad=sealed.purpose,
                        updated_at=audit.utc_stamp(),
                        updated_by=entry.actor_user_id,
                        updated_ip=entry.ip,
                    )
                )
                return
            row.ciphertext = sealed.ciphertext
            row.nonce = sealed.nonce
            row.key_version = sealed.key_version
            row.aad = sealed.purpose
            row.updated_at = audit.utc_stamp()
            row.updated_by = entry.actor_user_id
            row.updated_ip = entry.ip

        return await self._commit(apply, entry)

    async def reset(self, key: str, *, entry: AuditEntry) -> int:
        """Remove a stored value from either table; a reset is idempotent."""

        async def apply(session: AsyncSession) -> None:
            await session.execute(delete(ConfigOverride).where(ConfigOverride.config_key == key))
            await session.execute(delete(ConfigSecret).where(ConfigSecret.config_key == key))

        return await self._commit(apply, entry)

    async def _commit(
        self, apply: Callable[[AsyncSession], Awaitable[None]], entry: AuditEntry
    ) -> int:
        async with self._database.session() as session:
            await apply(session)
            audit.record(session, entry)
            await session.execute(
                update(ConfigState)
                .where(ConfigState.id == 1)
                .values(
                    version=ConfigState.version + 1,
                    updated_at=audit.utc_stamp(),
                    updated_by=entry.actor_user_id,
                )
            )
            version = await session.scalar(select(ConfigState.version).where(ConfigState.id == 1))
            await session.commit()
        return int(version or 1)


__all__ = ["ConfigRepository"]
