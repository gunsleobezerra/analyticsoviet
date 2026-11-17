"""SQLAlchemy models.

Two groups live here: the metric history table that predates the migration
runner, and the administrative/configuration tables. Every table is created by a
migration file in ``db/migrations``; the models exist so the application can
query them, and so a test can build the schema from metadata and compare it with
the schema the migrations produce.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ProfileSnapshot(Base):
    """One observation of a profile's audience metrics."""

    __tablename__ = "profile_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    handle: Mapped[str] = mapped_column(String(128), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    followers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    following: Mapped[int | None] = mapped_column(Integer, nullable=True)
    posts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    likes_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    engagement_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)

    __table_args__ = (
        Index("ix_snapshot_lookup", "platform", "handle", "captured_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ProfileSnapshot {self.platform}/{self.handle} "
            f"followers={self.followers} at={self.captured_at:%Y-%m-%dT%H:%M:%S}>"
        )


class AdminUser(Base):
    """The operator account allowed to open the admin panel."""

    __tablename__ = "admin_users"
    __table_args__ = (
        CheckConstraint("password_algo IN ('argon2id')", name="ck_admin_users_algo"),
        CheckConstraint("must_change_password IN (0, 1)", name="ck_admin_users_must_change"),
        CheckConstraint("is_active IN (0, 1)", name="ck_admin_users_active"),
        Index("ux_admin_users_username", "username", unique=True),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    username: Mapped[str] = mapped_column(Text(collation="NOCASE"), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    password_algo: Mapped[str] = mapped_column(Text, nullable=False)
    must_change_password: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    password_changed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class ConfigState(Base):
    """Single row holding the configuration version counter.

    Every write to an override or a secret bumps ``version``; workers compare it
    with their in-memory snapshot to know whether they are current.
    """

    __tablename__ = "config_state"
    __table_args__ = (CheckConstraint("id = 1", name="ck_config_state_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )


class ConfigOverride(Base):
    """A non-secret field set from the panel, stored as JSON text."""

    __tablename__ = "config_overrides"
    __table_args__ = (
        CheckConstraint("key = upper(key)", name="ck_config_overrides_key_upper"),
        CheckConstraint("length(key) BETWEEN 1 AND 64", name="ck_config_overrides_key_len"),
    )

    config_key: Mapped[str] = mapped_column("key", Text, primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False
    )
    updated_ip: Mapped[str | None] = mapped_column(Text, nullable=True)


class ConfigSecret(Base):
    """A secret field, stored only as ciphertext.

    ``aad`` binds the row to its purpose, so a ciphertext lifted from another
    table cannot be replayed here.
    """

    __tablename__ = "config_secrets"
    __table_args__ = (
        CheckConstraint("key = upper(key)", name="ck_config_secrets_key_upper"),
        CheckConstraint("length(key) BETWEEN 1 AND 64", name="ck_config_secrets_key_len"),
        CheckConstraint("length(nonce) = 12", name="ck_config_secrets_nonce_len"),
        CheckConstraint("aad = 'config_secret'", name="ck_config_secrets_aad"),
    )

    config_key: Mapped[str] = mapped_column("key", Text, primary_key=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    aad: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'config_secret'")
    )
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False
    )
    updated_ip: Mapped[str | None] = mapped_column(Text, nullable=True)


class AdminSession(Base):
    """A panel session: the cookie carries the id, the CSRF token is hashed here."""

    __tablename__ = "admin_sessions"
    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="ck_admin_sessions_expiry"),
        Index("ix_admin_sessions_user", "user_id", "expires_at"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False
    )
    csrf_token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    last_seen_at: Mapped[str] = mapped_column(Text, nullable=False)
    revoked_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)


class AdminAuthAttempt(Base):
    """One login attempt, used to throttle brute force per IP and per user."""

    __tablename__ = "admin_auth_attempts"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('success', 'bad_password', 'unknown_user', 'locked', 'csrf_fail')",
            name="ck_admin_auth_attempts_outcome",
        ),
        Index("ix_auth_attempts_ip", "ip", "attempted_at"),
        Index("ix_auth_attempts_user", "username", "attempted_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str] = mapped_column(Text, nullable=False)
    attempted_at: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)


class ConfigAuditLog(Base):
    """Append-only record of every configuration write.

    The last CHECK is the final guard: a secret row may carry only masked
    values, so a caller bug cannot leak a secret into the audit trail.
    """

    __tablename__ = "config_audit_log"
    __table_args__ = (
        CheckConstraint("action IN ('set', 'reset', 'reset_section')", name="ck_audit_action"),
        CheckConstraint("source IN ('panel', 'cli', 'api')", name="ck_audit_source"),
        CheckConstraint(
            "applied_mode IN ('hot', 'restart_required', 'rejected')", name="ck_audit_applied_mode"
        ),
        CheckConstraint("is_secret IN (0, 1)", name="ck_audit_is_secret"),
        CheckConstraint(
            "is_secret = 0 OR (old_value IS NULL OR old_value = '***') "
            "AND (new_value IS NULL OR new_value = '***')",
            name="ck_audit_secret_masked",
        ),
        Index("ix_audit_recent", "changed_at"),
        Index("ix_audit_key", "key", "changed_at"),
        Index("ix_audit_request", "request_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    changed_at: Mapped[str] = mapped_column(Text, nullable=False)
    actor_user_id: Mapped[str] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False
    )
    actor_username: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    config_key: Mapped[str] = mapped_column("key", Text, nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_secret: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    source: Mapped[str] = mapped_column(Text, nullable=False)
    applied_mode: Mapped[str] = mapped_column(Text, nullable=False)
    ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
