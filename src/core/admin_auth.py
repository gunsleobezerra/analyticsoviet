"""Password verification, session rules and login throttling for the admin panel.

Argon2id is the only hash the panel accepts and the hash lives in the database:
the environment supplies the first credential and nothing more. Verification is
deliberately expensive, so every call runs in a worker thread instead of on the
event loop.

This module holds the rules as data plus arithmetic over timestamps. The
statements live in ``db.admin_repository`` and the HTTP surface in
``api.security``.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from core.config import Settings
from core.logging import get_logger
from db.admin_repository import AdminRepository

LOGGER = get_logger("admin.auth")

#: OWASP baseline for Argon2id: 19 MiB of memory, two passes, 32-byte output.
PASSWORD_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19456,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)

#: Timestamp layout shared with the rest of the schema: UTC, microsecond precision.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S.%f"

#: Floor for a new password. A technical minimum, not a product rule: it keeps a
#: typo or a stray keypress from becoming the operator's credential.
MIN_PASSWORD_LENGTH = 8

#: Outcomes recorded in ``admin_auth_attempts``; the schema accepts no others.
OUTCOME_SUCCESS = "success"
OUTCOME_BAD_PASSWORD = "bad_password"
OUTCOME_UNKNOWN_USER = "unknown_user"
OUTCOME_LOCKED = "locked"
OUTCOME_CSRF_FAIL = "csrf_fail"
FAILURE_OUTCOMES = (OUTCOME_BAD_PASSWORD, OUTCOME_UNKNOWN_USER)

#: Cookies the panel sets. The session id is unreadable to scripts; the CSRF
#: token has to be readable, because the page echoes it in a header.
SESSION_COOKIE = "admin_session"
CSRF_COOKIE = "admin_csrf"
CSRF_HEADER = "X-CSRF-Token"
COOKIE_PATH = "/admin"


def utcnow() -> datetime:
    return datetime.now(UTC)


def stamp(moment: datetime) -> str:
    """Format a moment the way every administrative table stores it."""
    return moment.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def parse_stamp(text: str) -> datetime:
    """Read back a stored timestamp as an aware UTC datetime."""
    return datetime.strptime(text, TIMESTAMP_FORMAT).replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# passwords
# --------------------------------------------------------------------------- #


def hash_password(password: str) -> str:
    return PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Check a password against its hash.

    Every failure mode is a rejection: a malformed hash must not raise into the
    request handler, or a corrupted row would answer with a 500 instead of a
    plain "wrong credentials".
    """
    try:
        PASSWORD_HASHER.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return True


def needs_rehash(password_hash: str) -> bool:
    """Whether the stored hash uses weaker parameters than the ones in force."""
    try:
        return PASSWORD_HASHER.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def is_argon2id(password_hash: str) -> bool:
    """Whether a value is a well-formed Argon2id hash."""
    try:
        PASSWORD_HASHER.check_needs_rehash(password_hash)
    except InvalidHashError:
        return False
    return password_hash.startswith("$argon2id$")


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """A hash of a random throwaway password, used only to spend time."""
    return PASSWORD_HASHER.hash(secrets.token_urlsafe(32))


def verify_against_dummy(password: str) -> None:
    """Spend the same work as a real check when the username does not exist.

    Without this, an unknown user answers faster than a known one with a wrong
    password, and the response time tells an attacker which accounts exist.
    """
    verify_password(_dummy_hash(), password)


# --------------------------------------------------------------------------- #
# tokens
# --------------------------------------------------------------------------- #


def new_token() -> str:
    """A 256-bit URL-safe token, used for session ids and CSRF tokens."""
    return secrets.token_urlsafe(32)


def token_fingerprint(token: str) -> str:
    """What the database keeps instead of the token itself."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return secrets.compare_digest(left, right)


# --------------------------------------------------------------------------- #
# policies
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LoginPolicy:
    """How many failures, over how long, and for how long the door stays shut."""

    max_failures: int = 5
    window_seconds: int = 900
    lockout_seconds: int = 900

    def window_start(self, now: datetime) -> datetime:
        return now - timedelta(seconds=self.window_seconds)

    def is_locked(self, failures: list[datetime], now: datetime) -> bool:
        """Lock after ``max_failures`` inside the window, for ``lockout_seconds``.

        The lockout counts from the last failure, so a burst that ends with a
        wrong guess cannot be followed straight away by a correct one.
        """
        if len(failures) < self.max_failures:
            return False
        return now - failures[-1] < timedelta(seconds=self.lockout_seconds)


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    """Idle timeout with a sliding renewal, capped by an absolute lifetime."""

    idle_seconds: int = 28800
    absolute_seconds: int = 86400

    def expiry(self, created_at: datetime, now: datetime) -> datetime:
        """When a session opened at ``created_at`` and used at ``now`` dies."""
        ceiling = created_at + timedelta(seconds=self.absolute_seconds)
        sliding = now + timedelta(seconds=self.idle_seconds)
        return min(ceiling, sliding)


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #


class SeedOutcome(StrEnum):
    """What the boot did about the administrator row."""

    EXISTING_USER = "existing_user"
    FROM_HASH = "from_hash"
    FROM_PASSWORD = "from_password"
    NO_CREDENTIAL = "no_credential"
    INVALID_HASH = "invalid_hash"


@dataclass(slots=True)
class AdminSessionView:
    """One authenticated request: the row plus the user it belongs to."""

    session_id: str
    user_id: str
    username: str
    must_change_password: bool
    csrf_fingerprint: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime


class LoginRefused(Exception):
    """A login that did not open a session; ``status_code`` is what the client sees."""

    def __init__(self, reason: str, status_code: int = 401) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


class CsrfRefused(Exception):
    """A write that arrived without a matching CSRF token."""


class AdminApiError(Exception):
    """A request the panel answers with a typed error envelope.

    Carried as an exception so a dependency and a route can fail the same way,
    and rendered by one handler in the router module.
    """

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message


class PasswordChangeRefused(Exception):
    """A password change that did not go through."""

    def __init__(self, reason: str, status_code: int = 422) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


# --------------------------------------------------------------------------- #
# service
# --------------------------------------------------------------------------- #


class AdminAuthService:
    """Seeds the administrator, opens and validates sessions, throttles logins."""

    def __init__(
        self,
        settings: Settings,
        repository: AdminRepository,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.clock = clock
        self.login_policy = LoginPolicy(
            max_failures=settings.admin_login_max_failures,
            window_seconds=settings.admin_login_window_seconds,
            lockout_seconds=settings.admin_lockout_seconds,
        )
        self.session_policy = SessionPolicy(
            idle_seconds=settings.admin_session_idle_seconds,
            absolute_seconds=settings.admin_session_absolute_seconds,
        )
        self.seed_outcome: SeedOutcome | None = None

    # -- boot ------------------------------------------------------------- #

    async def seed(self) -> SeedOutcome:
        """Create the administrator row when the table is empty.

        The environment is the source of the *first* credential only. Once a row
        exists the database is the truth, so restarting the service never
        resurrects a password that was changed in the panel.

        Nothing here raises on a missing credential: an instance without a way to
        log in still serves the public API, and the panel refuses logins. The
        reason is logged and kept in ``seed_outcome``.
        """
        if await self.repository.count_users() > 0:
            self.seed_outcome = SeedOutcome.EXISTING_USER
            return self.seed_outcome

        username = self.settings.admin_username
        configured_hash = (self.settings.admin_password_hash or "").strip()
        plain = self.settings.admin_password

        if configured_hash:
            if not is_argon2id(configured_hash):
                self.seed_outcome = SeedOutcome.INVALID_HASH
                LOGGER.error(
                    "admin_seed_failed",
                    reason="the configured password hash is not a valid argon2id hash",
                )
                return self.seed_outcome
            password_hash = configured_hash
            source = SeedOutcome.FROM_HASH
        elif plain:
            password_hash = hash_password(plain)
            source = SeedOutcome.FROM_PASSWORD
        else:
            self.seed_outcome = SeedOutcome.NO_CREDENTIAL
            LOGGER.warning(
                "admin_seed_skipped",
                reason="no initial credential in the environment; the panel will refuse logins",
                username=username,
            )
            return self.seed_outcome

        now = self.clock()
        await self.repository.add_user(
            user_id=new_token(),
            username=username,
            password_hash=password_hash,
            created_at=stamp(now),
        )
        self.seed_outcome = source
        LOGGER.info(
            "admin_seeded",
            source=str(source),
            username=username,
            must_change_password=True,
        )
        return self.seed_outcome

    # -- login ------------------------------------------------------------ #

    async def login(
        self,
        *,
        username: str,
        password: str,
        ip: str,
        user_agent: str | None,
        forwarded_proto: str | None,
    ) -> tuple[AdminSessionView, str]:
        """Authenticate and open a session.

        Returns the session view and the CSRF token in clear, which the caller
        puts in a readable cookie and in the response body; only its fingerprint
        reaches the database.

        Every rejection is deliberately identical to the client. The reason is
        for the log and for ``admin_auth_attempts``, never for the response:
        telling a wrong password apart from an unknown user hands an attacker a
        list of valid usernames.
        """
        if not self._transport_is_secure(forwarded_proto):
            LOGGER.warning(
                "admin_login_refused",
                reason="request did not arrive over https while running in production",
            )
            raise LoginRefused("insecure_transport", status_code=403)

        now = self.clock()
        if await self.repository.count_users() == 0:
            LOGGER.warning("admin_login_refused", reason="no administrator is configured")
            raise LoginRefused("no_admin")

        failures = [
            parse_stamp(value)
            for value in await self.repository.failure_stamps(
                ip=ip,
                username=username,
                since=stamp(self.login_policy.window_start(now)),
            )
        ]
        if self.login_policy.is_locked(failures, now):
            await self._record_attempt(username, ip, user_agent, OUTCOME_LOCKED, now)
            LOGGER.warning("admin_login_refused", reason="too many failures, locked", username=username)
            raise LoginRefused("locked")

        user = await self.repository.find_user(username)
        if user is None:
            verify_against_dummy(password)
            await self._record_attempt(username, ip, user_agent, OUTCOME_UNKNOWN_USER, now)
            raise LoginRefused("invalid_credentials")

        if not user.is_active:
            await self._record_attempt(username, ip, user_agent, OUTCOME_LOCKED, now)
            LOGGER.warning("admin_login_refused", reason="account is inactive", username=username)
            raise LoginRefused("invalid_credentials")

        if not verify_password(user.password_hash, password):
            await self._record_attempt(username, ip, user_agent, OUTCOME_BAD_PASSWORD, now)
            raise LoginRefused("invalid_credentials")

        if needs_rehash(user.password_hash):
            # Parameters got stronger since the hash was written; rewrite it now
            # that the password is known, without asking the operator for anything.
            await self.repository.store_password(
                user.id,
                hash_password(password),
                changed_at=stamp(now),
                clear_must_change=False,
            )

        session_id = new_token()
        csrf_token = new_token()
        expires_at = self.session_policy.expiry(now, now)
        await self.repository.add_session(
            session_id=session_id,
            user_id=user.id,
            csrf_fingerprint=token_fingerprint(csrf_token),
            created_at=stamp(now),
            last_seen_at=stamp(now),
            expires_at=stamp(expires_at),
            ip=ip,
            user_agent=user_agent,
        )
        await self._record_attempt(username, ip, user_agent, OUTCOME_SUCCESS, now)
        LOGGER.info("admin_login", username=user.username, ip=ip)

        view = AdminSessionView(
            session_id=session_id,
            user_id=user.id,
            username=user.username,
            must_change_password=bool(user.must_change_password),
            csrf_fingerprint=token_fingerprint(csrf_token),
            created_at=now,
            last_seen_at=now,
            expires_at=expires_at,
        )
        return view, csrf_token

    # -- session ---------------------------------------------------------- #

    async def open_session(self, session_id: str | None) -> AdminSessionView | None:
        """Resolve a session id into an authenticated request, sliding its expiry.

        Returns ``None`` for anything that is not a live session: unknown,
        revoked, past its idle timeout or past its absolute ceiling.
        """
        if not session_id:
            return None

        row = await self.repository.find_session(session_id)
        if row is None or row.revoked_at is not None:
            return None

        now = self.clock()
        created_at = parse_stamp(row.created_at)
        last_seen_at = parse_stamp(row.last_seen_at)
        expires_at = parse_stamp(row.expires_at)

        if now >= expires_at:
            return None
        if now - last_seen_at >= timedelta(seconds=self.session_policy.idle_seconds):
            return None

        user = await self.repository.find_user_by_id(row.user_id)
        if user is None or not user.is_active:
            return None

        renewed = self.session_policy.expiry(created_at, now)
        await self.repository.touch_session(
            row.id, last_seen_at=stamp(now), expires_at=stamp(renewed)
        )

        return AdminSessionView(
            session_id=row.id,
            user_id=user.id,
            username=user.username,
            must_change_password=bool(user.must_change_password),
            csrf_fingerprint=row.csrf_token_hash,
            created_at=created_at,
            last_seen_at=now,
            expires_at=renewed,
        )

    async def logout(self, session: AdminSessionView) -> None:
        await self.repository.revoke_session(session.session_id, at=stamp(self.clock()))

    # -- csrf ------------------------------------------------------------- #

    def check_csrf(
        self,
        session: AdminSessionView,
        *,
        header_token: str | None,
        cookie_token: str | None,
    ) -> None:
        """Double-submit check: header and cookie must agree, and match the session.

        The cookie alone is not enough (another site can send it), the header
        alone is not enough (it must come from a page that can read the cookie),
        and the fingerprint ties both to this session, so a token from another
        session does not pass.
        """
        if not tokens_match(header_token, cookie_token):
            raise CsrfRefused("header and cookie disagree")
        if not tokens_match(token_fingerprint(header_token or ""), session.csrf_fingerprint):
            raise CsrfRefused("token does not belong to this session")

    async def record_csrf_failure(
        self, *, username: str | None, ip: str, user_agent: str | None
    ) -> None:
        await self._record_attempt(username, ip, user_agent, OUTCOME_CSRF_FAIL, self.clock())

    # -- password change -------------------------------------------------- #

    async def change_password(
        self,
        session: AdminSessionView,
        *,
        current_password: str,
        new_password: str,
    ) -> None:
        """Replace the password and clear the "must change" flag.

        This is the only path that clears the flag, and it revokes every session
        of the user, including the current one: a credential that was possibly
        known to someone else must not keep a live session alive.
        """
        if len(new_password.strip()) < MIN_PASSWORD_LENGTH:
            raise PasswordChangeRefused("new password is too short")
        if new_password == current_password:
            raise PasswordChangeRefused("new password equals the current one")

        user = await self.repository.find_user_by_id(session.user_id)
        if user is None:
            raise LoginRefused("invalid_credentials")
        if not verify_password(user.password_hash, current_password):
            now = self.clock()
            await self._record_attempt(user.username, "-", None, OUTCOME_BAD_PASSWORD, now)
            raise PasswordChangeRefused("current password is wrong", status_code=401)

        now = self.clock()
        await self.repository.store_password(
            user.id,
            hash_password(new_password),
            changed_at=stamp(now),
            clear_must_change=True,
        )
        await self.repository.revoke_user_sessions(user.id, at=stamp(now))
        LOGGER.info("admin_password_changed", username=user.username)

    # -- internals -------------------------------------------------------- #

    def _transport_is_secure(self, forwarded_proto: str | None) -> bool:
        """Production only accepts a login that arrived over TLS.

        The value comes from ``X-Forwarded-Proto``, which is the only signal a
        process behind the reverse proxy has. A missing or different value fails
        closed instead of silently issuing a cookie that would travel in clear.
        """
        if self.settings.environment != "prod":
            return True
        return (forwarded_proto or "").split(",")[0].strip().lower() == "https"

    async def _record_attempt(
        self,
        username: str | None,
        ip: str,
        user_agent: str | None,
        outcome: str,
        at: datetime,
    ) -> None:
        await self.repository.record_attempt(
            username=username,
            ip=ip,
            outcome=outcome,
            user_agent=user_agent,
            at=stamp(at),
        )
