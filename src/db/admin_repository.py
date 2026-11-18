"""Statements for the administrator account, its sessions and login attempts.

Timestamps travel as already-formatted strings, the same text the schema stores,
so this module never has to know how a moment is written down and the service
that decides the rules never touches SQL.
"""

from __future__ import annotations

from sqlalchemy import func, select, update

from db.models import AdminAuthAttempt, AdminSession, AdminUser
from db.session import Database

#: The only value the schema accepts in ``password_algo``.
PASSWORD_ALGO = "argon2id"

#: Outcomes that count towards the lockout, mirroring the service's policy.
_FAILURE_OUTCOMES = ("bad_password", "unknown_user")


class AdminRepository:
    """Reads and writes the administrative tables."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def count_users(self) -> int:
        async with self.database.session() as session:
            total = await session.scalar(select(func.count()).select_from(AdminUser))
            return int(total or 0)

    async def add_user(
        self,
        *,
        user_id: str,
        username: str,
        password_hash: str,
        created_at: str,
    ) -> None:
        """Insert the single operator, always flagged for a first password change."""
        async with self.database.session() as session:
            session.add(
                AdminUser(
                    id=user_id,
                    username=username,
                    password_hash=password_hash,
                    password_algo=PASSWORD_ALGO,
                    must_change_password=1,
                    is_active=1,
                    password_changed_at=None,
                    created_at=created_at,
                    updated_at=created_at,
                )
            )
            await session.commit()

    async def find_user(self, username: str) -> AdminUser | None:
        """Look a user up by name; the column collation makes this case-insensitive."""
        async with self.database.session() as session:
            result = await session.execute(select(AdminUser).where(AdminUser.username == username))
            return result.scalar_one_or_none()

    async def find_user_by_id(self, user_id: str) -> AdminUser | None:
        async with self.database.session() as session:
            return await session.get(AdminUser, user_id)

    async def store_password(
        self,
        user_id: str,
        password_hash: str,
        *,
        changed_at: str,
        clear_must_change: bool,
    ) -> None:
        """Write a new hash.

        ``clear_must_change`` separates the two callers: a change made by the
        operator fulfils the pending requirement, a rehash during login does not
        — it only refreshes the parameters of a password that is already in use.
        """
        values: dict[str, object] = {
            "password_hash": password_hash,
            "password_algo": PASSWORD_ALGO,
            "updated_at": changed_at,
        }
        if clear_must_change:
            values["must_change_password"] = 0
            values["password_changed_at"] = changed_at
        async with self.database.session() as session:
            await session.execute(update(AdminUser).where(AdminUser.id == user_id).values(**values))
            await session.commit()

    async def record_attempt(
        self,
        *,
        username: str | None,
        ip: str,
        outcome: str,
        user_agent: str | None,
        at: str,
    ) -> None:
        async with self.database.session() as session:
            session.add(
                AdminAuthAttempt(
                    username=username,
                    ip=ip,
                    attempted_at=at,
                    outcome=outcome,
                    user_agent=user_agent,
                )
            )
            await session.commit()

    async def failure_stamps(self, *, ip: str, username: str, since: str) -> list[str]:
        """Timestamps of the failed attempts for this pair, oldest first."""
        statement = (
            select(AdminAuthAttempt.attempted_at)
            .where(
                AdminAuthAttempt.ip == ip,
                AdminAuthAttempt.outcome.in_(_FAILURE_OUTCOMES),
                AdminAuthAttempt.attempted_at >= since,
                func.lower(AdminAuthAttempt.username) == username.lower(),
            )
            .order_by(AdminAuthAttempt.attempted_at)
        )
        async with self.database.session() as session:
            result = await session.execute(statement)
            return list(result.scalars().all())

    async def add_session(
        self,
        *,
        session_id: str,
        user_id: str,
        csrf_fingerprint: str,
        created_at: str,
        last_seen_at: str,
        expires_at: str,
        ip: str | None,
        user_agent: str | None,
    ) -> None:
        async with self.database.session() as session:
            session.add(
                AdminSession(
                    id=session_id,
                    user_id=user_id,
                    csrf_token_hash=csrf_fingerprint,
                    created_at=created_at,
                    expires_at=expires_at,
                    last_seen_at=last_seen_at,
                    revoked_at=None,
                    ip=ip,
                    user_agent=user_agent,
                )
            )
            await session.commit()

    async def find_session(self, session_id: str) -> AdminSession | None:
        async with self.database.session() as session:
            return await session.get(AdminSession, session_id)

    async def touch_session(self, session_id: str, *, last_seen_at: str, expires_at: str) -> None:
        async with self.database.session() as session:
            await session.execute(
                update(AdminSession)
                .where(AdminSession.id == session_id)
                .values(last_seen_at=last_seen_at, expires_at=expires_at)
            )
            await session.commit()

    async def revoke_session(self, session_id: str, *, at: str) -> None:
        async with self.database.session() as session:
            await session.execute(
                update(AdminSession)
                .where(AdminSession.id == session_id, AdminSession.revoked_at.is_(None))
                .values(revoked_at=at)
            )
            await session.commit()

    async def revoke_user_sessions(self, user_id: str, *, at: str) -> None:
        async with self.database.session() as session:
            await session.execute(
                update(AdminSession)
                .where(AdminSession.user_id == user_id, AdminSession.revoked_at.is_(None))
                .values(revoked_at=at)
            )
            await session.commit()
