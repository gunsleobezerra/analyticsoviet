"""Audit trail for configuration writes.

Every write appends one row per changed field. The trail is the only history the
system keeps: it is what lets an operator answer "who changed this, when, from
what to what" without a versioned copy of the whole configuration.

A secret field is recorded as changed, never as a value. The masking happens
here and the schema refuses anything else, so a caller bug cannot leak a secret
into the trail.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from core.config_store import AuditEntry
from db.models import ConfigAuditLog

#: What the trail stores in place of a secret value.
MASK = "***"


def utc_stamp() -> str:
    """Timestamp in the format the schema stores everywhere else."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def masked(value: str | None, *, is_secret: bool) -> str | None:
    """Replace a secret value with the mask, keeping "unset" distinguishable."""
    if value is None:
        return None
    return MASK if is_secret else value


def build_row(entry: AuditEntry) -> ConfigAuditLog:
    return ConfigAuditLog(
        changed_at=utc_stamp(),
        actor_user_id=entry.actor_user_id,
        actor_username=entry.actor_username,
        action=entry.action,
        config_key=entry.config_key,
        old_value=masked(entry.old_value, is_secret=entry.is_secret),
        new_value=masked(entry.new_value, is_secret=entry.is_secret),
        is_secret=1 if entry.is_secret else 0,
        source=entry.source,
        applied_mode=entry.applied_mode,
        ip=entry.ip,
        request_id=entry.request_id,
    )


def record(session: AsyncSession, entry: AuditEntry) -> None:
    """Stage the audit row in the caller's transaction.

    It must join the same transaction as the write it describes: an audit row
    for a write that rolled back would be a lie, and a write without its row
    would be untraceable.
    """
    session.add(build_row(entry))


__all__ = ["MASK", "masked", "record", "utc_stamp"]
