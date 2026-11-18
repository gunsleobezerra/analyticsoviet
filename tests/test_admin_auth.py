"""Tests for the panel's authentication.

Everything runs without a network and on a temporary SQLite database. What is
pinned here is the set of promises the rest of the panel leans on: the
credential is seeded once and hashed, a wrong password and an unknown user are
indistinguishable, repeated failures lock the pair, a session dies on idle and on
its absolute ceiling, a write needs the CSRF token, and a pending password change
blocks configuration writes.

Time is moved by hand: the service takes a clock, so an eight-hour idle window is
a test that costs microseconds instead of a test that sleeps.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from api.admin_ui import ASSETS_DIR
from api.deps import require_password_changed
from core.admin_auth import (
    AdminAuthService,
    LoginPolicy,
    LoginRefused,
    SeedOutcome,
    hash_password,
    is_argon2id,
)
from core.config import Settings
from db import migrate
from db.admin_repository import AdminRepository
from main import create_app

OPERATOR = "operador"
PASSWORD = "senha-inicial"
NEW_PASSWORD = "senha-nova-1"

LOGIN = "/admin/api/login"
SESSION = "/admin/api/session"
LOGOUT = "/admin/api/logout"
PASSWORD_ROUTE = "/admin/api/password"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, start: datetime) -> None:
        self.moment = start

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **delta: float) -> None:
        self.moment += timedelta(**delta)


def build_settings(url: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "enable_ytdlp_fallback": False,
        "database_url": url,
        "admin_username": OPERATOR,
        "admin_password": PASSWORD,
    }
    values.update(overrides)
    return Settings(**values)


def sqlite_path(url: str) -> Path:
    return Path(url.split("///", 1)[1])


def query(url: str, statement: str) -> list[tuple]:
    with sqlite3.connect(sqlite_path(url)) as connection:
        return list(connection.execute(statement))


def count(url: str, table: str) -> int:
    return int(query(url, f"select count(*) from {table}")[0][0])


def database_text(url: str) -> str:
    """Every stored row of every administrative table, as text."""
    tables = ("admin_users", "admin_sessions", "admin_auth_attempts", "config_audit_log")
    with sqlite3.connect(sqlite_path(url)) as connection:
        rows: list[str] = []
        for table in tables:
            rows.extend(repr(row) for row in connection.execute(f"select * from {table}"))
        return "\n".join(rows)


def sign_in(client: TestClient, *, password: str = PASSWORD, username: str = OPERATOR) -> dict:
    response = client.post(LOGIN, json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def csrf_token(client: TestClient) -> str:
    token = client.cookies.get("admin_csrf")
    assert token
    return token


def with_clock(client: TestClient, start: datetime) -> Clock:
    """Replace the service the app holds with one whose clock the test controls."""
    state = client.app.state.app_state
    clock = Clock(start)
    state.admin = AdminAuthService(state.settings, AdminRepository(state.db), clock=clock)
    return clock


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'admin.db').as_posix()}"
    migrate.run_migrations(url)
    return url


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    application = create_app(build_settings(database_url))
    with TestClient(application) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# boot: the initial credential
# --------------------------------------------------------------------------- #


def test_the_first_boot_hashes_the_configured_password(client: TestClient, database_url: str) -> None:
    service = client.app.state.app_state.admin
    assert service is not None
    assert service.seed_outcome is SeedOutcome.FROM_PASSWORD

    rows = query(
        database_url,
        "select username, password_hash, password_algo, must_change_password, is_active"
        " from admin_users",
    )
    assert len(rows) == 1
    username, password_hash, algo, must_change, active = rows[0]
    assert username == OPERATOR
    assert algo == "argon2id"
    assert must_change == 1
    assert active == 1
    assert password_hash.startswith("$argon2id$")
    assert is_argon2id(password_hash)
    assert password_hash != PASSWORD
    assert PASSWORD not in database_text(database_url)


def test_the_configured_hash_wins_over_the_plain_password(database_url: str) -> None:
    configured = hash_password("hash-do-ambiente")
    application = create_app(build_settings(database_url, admin_password_hash=configured))

    with TestClient(application) as client:
        service = application.state.app_state.admin
        assert service is not None
        assert service.seed_outcome is SeedOutcome.FROM_HASH
        # The hash is the credential; the plain value in the environment is ignored.
        assert client.post(LOGIN, json={"username": OPERATOR, "password": PASSWORD}).status_code == 401
        assert client.post(LOGIN, json={"username": OPERATOR, "password": "hash-do-ambiente"}).status_code == 200

    assert query(database_url, "select password_hash from admin_users")[0][0] == configured


def test_a_boot_without_credentials_serves_and_refuses_login(database_url: str) -> None:
    application = create_app(build_settings(database_url, admin_password=None))

    with TestClient(application) as client:
        service = application.state.app_state.admin
        assert service is not None
        assert service.seed_outcome is SeedOutcome.NO_CREDENTIAL
        assert client.get("/v1/health").status_code == 200
        response = client.post(LOGIN, json={"username": OPERATOR, "password": PASSWORD})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_credentials"

    assert count(database_url, "admin_users") == 0
    assert count(database_url, "admin_sessions") == 0


def test_a_malformed_configured_hash_is_refused_at_boot(database_url: str) -> None:
    application = create_app(build_settings(database_url, admin_password_hash="not-a-hash"))

    with TestClient(application) as client:
        service = application.state.app_state.admin
        assert service is not None
        assert service.seed_outcome is SeedOutcome.INVALID_HASH
        assert client.get("/v1/health").status_code == 200

    assert count(database_url, "admin_users") == 0


# --------------------------------------------------------------------------- #
# login
# --------------------------------------------------------------------------- #


def test_a_valid_login_opens_a_session(client: TestClient) -> None:
    response = client.post(LOGIN, json={"username": OPERATOR, "password": PASSWORD})
    assert response.status_code == 200
    body = response.json()
    assert body["username"] == OPERATOR
    assert body["must_change_password"] is True
    assert body["csrf_token"]

    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(value for value in cookies if value.startswith("admin_session="))
    csrf_cookie = next(value for value in cookies if value.startswith("admin_csrf="))
    assert "HttpOnly" in session_cookie
    assert "samesite=lax" in session_cookie.lower()
    assert "HttpOnly" not in csrf_cookie
    # Not production: a Secure cookie would never come back over http://localhost.
    assert "Secure" not in session_cookie

    session = client.get(SESSION)
    assert session.status_code == 200
    assert session.json()["username"] == OPERATOR


def test_the_username_is_case_insensitive(client: TestClient) -> None:
    assert client.post(LOGIN, json={"username": OPERATOR.upper(), "password": PASSWORD}).status_code == 200


def test_a_wrong_password_and_an_unknown_user_answer_the_same(client: TestClient) -> None:
    wrong = client.post(LOGIN, json={"username": OPERATOR, "password": "errada"})
    unknown = client.post(LOGIN, json={"username": "ninguem", "password": "errada"})

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_a_request_without_a_session_is_refused(client: TestClient) -> None:
    response = client.get(SESSION)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


# --------------------------------------------------------------------------- #
# brute force
# --------------------------------------------------------------------------- #


def test_five_failures_lock_the_pair_and_the_sixth_stays_generic(
    client: TestClient, database_url: str
) -> None:
    with_clock(client, datetime.now(UTC))

    for _ in range(5):
        assert client.post(LOGIN, json={"username": OPERATOR, "password": "errada"}).status_code == 401

    # Even the right password is refused while the pair is locked.
    sixth = client.post(LOGIN, json={"username": OPERATOR, "password": PASSWORD})
    assert sixth.status_code == 401
    assert sixth.json()["error"]["code"] == "invalid_credentials"

    outcomes = [row[0] for row in query(database_url, "select outcome from admin_auth_attempts order by id")]
    assert outcomes.count("bad_password") == 5
    assert outcomes[-1] == "locked"
    assert count(database_url, "admin_sessions") == 0


def test_the_lock_lifts_after_its_window(client: TestClient) -> None:
    clock = with_clock(client, datetime.now(UTC))

    for _ in range(5):
        client.post(LOGIN, json={"username": OPERATOR, "password": "errada"})
    assert client.post(LOGIN, json={"username": OPERATOR, "password": PASSWORD}).status_code == 401

    clock.advance(minutes=16)
    assert client.post(LOGIN, json={"username": OPERATOR, "password": PASSWORD}).status_code == 200


async def test_the_throttle_is_keyed_by_address_and_user(client: TestClient) -> None:
    clock = with_clock(client, datetime.now(UTC))
    service = client.app.state.app_state.admin
    assert service is not None

    for _ in range(5):
        with pytest.raises(LoginRefused):
            await service.login(
                username=OPERATOR,
                password="errada",
                ip="198.51.100.7",
                user_agent=None,
                forwarded_proto=None,
            )

    with pytest.raises(LoginRefused):
        await service.login(
            username=OPERATOR,
            password=PASSWORD,
            ip="198.51.100.7",
            user_agent=None,
            forwarded_proto=None,
        )

    # Another address has its own bucket for the same account.
    view, _ = await service.login(
        username=OPERATOR,
        password=PASSWORD,
        ip="198.51.100.8",
        user_agent=None,
        forwarded_proto=None,
    )
    assert view.username == OPERATOR
    assert view.created_at == clock.moment


def test_the_policy_counts_failures_inside_the_window() -> None:
    policy = LoginPolicy()
    now = datetime.now(UTC)

    assert not policy.is_locked([now - timedelta(minutes=1)] * 4, now)
    assert policy.is_locked([now - timedelta(minutes=1)] * 5, now)
    # The same five failures, but the last one is older than the lockout.
    assert not policy.is_locked([now - timedelta(minutes=16)] * 5, now)


# --------------------------------------------------------------------------- #
# session lifetime
# --------------------------------------------------------------------------- #


def test_a_session_dies_after_the_idle_window(client: TestClient) -> None:
    clock = with_clock(client, datetime.now(UTC))
    sign_in(client)
    assert client.get(SESSION).status_code == 200

    clock.advance(hours=8, seconds=1)
    assert client.get(SESSION).status_code == 401


def test_a_session_slides_until_the_absolute_ceiling(client: TestClient) -> None:
    clock = with_clock(client, datetime.now(UTC))
    sign_in(client)

    # Each use renews the idle window: 7 h, 14 h and 21 h after the login.
    for _ in range(3):
        clock.advance(hours=7)
        assert client.get(SESSION).status_code == 200
    clock.advance(hours=2)
    assert client.get(SESSION).status_code == 200

    # At 24 hours the ceiling ends it, even though the last use was an hour ago
    # and the idle window would still allow it.
    clock.advance(hours=1)
    assert client.get(SESSION).status_code == 401


def test_logout_revokes_the_session(client: TestClient, database_url: str) -> None:
    sign_in(client)
    response = client.post(LOGOUT, headers={"X-CSRF-Token": csrf_token(client)})
    assert response.status_code == 200
    assert client.get(SESSION).status_code == 401
    assert query(database_url, "select revoked_at from admin_sessions")[0][0] is not None


# --------------------------------------------------------------------------- #
# transport and cookies in production
# --------------------------------------------------------------------------- #


def test_production_refuses_a_login_that_did_not_arrive_over_https(database_url: str) -> None:
    application = create_app(build_settings(database_url, environment="prod"))

    with TestClient(application) as client:
        refused = client.post(LOGIN, json={"username": OPERATOR, "password": PASSWORD})
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "https_required"

        accepted = client.post(
            LOGIN,
            json={"username": OPERATOR, "password": PASSWORD},
            headers={"x-forwarded-proto": "https"},
        )
        assert accepted.status_code == 200
        cookies = accepted.headers.get_list("set-cookie")
        assert all("Secure" in value for value in cookies)

    assert count(database_url, "admin_sessions") == 1


# --------------------------------------------------------------------------- #
# CSRF
# --------------------------------------------------------------------------- #


def test_a_write_without_the_csrf_token_is_refused(client: TestClient) -> None:
    sign_in(client)
    body = {"current_password": PASSWORD, "new_password": NEW_PASSWORD}

    missing = client.post(PASSWORD_ROUTE, json=body)
    assert missing.status_code == 403
    assert missing.json()["error"]["code"] == "csrf_failed"

    divergent = client.post(PASSWORD_ROUTE, json=body, headers={"X-CSRF-Token": "outro-token"})
    assert divergent.status_code == 403

    accepted = client.post(PASSWORD_ROUTE, json=body, headers={"X-CSRF-Token": csrf_token(client)})
    assert accepted.status_code == 200
    assert accepted.json()["relogin_required"] is True


def test_a_csrf_failure_is_recorded(client: TestClient, database_url: str) -> None:
    sign_in(client)
    client.post(PASSWORD_ROUTE, json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    outcomes = [row[0] for row in query(database_url, "select outcome from admin_auth_attempts")]
    assert "csrf_fail" in outcomes


def test_a_token_from_another_session_does_not_pass(client: TestClient) -> None:
    sign_in(client)
    stolen = csrf_token(client)
    client.post(LOGOUT, headers={"X-CSRF-Token": stolen})
    sign_in(client)

    response = client.post(
        PASSWORD_ROUTE,
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers={"X-CSRF-Token": stolen},
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# password change and the configuration-write gate
# --------------------------------------------------------------------------- #


def test_the_pending_password_blocks_a_configuration_write(client: TestClient) -> None:
    async def write_config() -> dict[str, bool]:
        return {"written": True}

    client.app.add_api_route(
        "/admin/api/probe-config",
        write_config,
        methods=["POST"],
        dependencies=[Depends(require_password_changed)],
        include_in_schema=False,
    )

    sign_in(client)
    token = csrf_token(client)

    pending = client.post("/admin/api/probe-config", headers={"X-CSRF-Token": token})
    assert pending.status_code == 422
    assert pending.json()["error"]["code"] == "password_change_required"

    changed = client.post(
        PASSWORD_ROUTE,
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers={"X-CSRF-Token": token},
    )
    assert changed.status_code == 200

    # The change revoked the session, so the next write needs a new login.
    assert client.get(SESSION).status_code == 401
    sign_in(client, password=NEW_PASSWORD)
    allowed = client.post("/admin/api/probe-config", headers={"X-CSRF-Token": csrf_token(client)})
    assert allowed.status_code == 200


def test_the_change_revokes_every_session_of_the_user(client: TestClient, database_url: str) -> None:
    sign_in(client)
    client.post(
        PASSWORD_ROUTE,
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers={"X-CSRF-Token": csrf_token(client)},
    )

    assert query(database_url, "select count(*) from admin_sessions where revoked_at is null")[0][0] == 0
    assert query(database_url, "select must_change_password from admin_users")[0][0] == 0
    assert query(database_url, "select password_changed_at from admin_users")[0][0] is not None


def test_a_short_or_repeated_password_is_refused(client: TestClient) -> None:
    sign_in(client)
    token = csrf_token(client)

    short = client.post(
        PASSWORD_ROUTE,
        json={"current_password": PASSWORD, "new_password": "abc"},
        headers={"X-CSRF-Token": token},
    )
    assert short.status_code == 422

    repeated = client.post(
        PASSWORD_ROUTE,
        json={"current_password": PASSWORD, "new_password": PASSWORD},
        headers={"X-CSRF-Token": token},
    )
    assert repeated.status_code == 422

    wrong_current = client.post(
        PASSWORD_ROUTE,
        json={"current_password": "errada", "new_password": NEW_PASSWORD},
        headers={"X-CSRF-Token": token},
    )
    assert wrong_current.status_code == 401


def test_no_clear_password_reaches_the_database(client: TestClient, database_url: str) -> None:
    sign_in(client)
    client.post(LOGIN, json={"username": OPERATOR, "password": "errada"})
    client.post(LOGIN, json={"username": "ninguem", "password": "errada"})
    client.post(
        PASSWORD_ROUTE,
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers={"X-CSRF-Token": csrf_token(client)},
    )

    stored = database_text(database_url)
    assert PASSWORD not in stored
    assert NEW_PASSWORD not in stored
    assert "errada" not in stored


# --------------------------------------------------------------------------- #
# the page itself
# --------------------------------------------------------------------------- #


def test_the_panel_page_is_served_from_local_assets(client: TestClient) -> None:
    page = client.get("/admin")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["content-security-policy"].startswith("default-src 'self'")

    for name, media_type in (("admin.css", "text/css"), ("admin.js", "application/javascript")):
        asset = client.get(f"/admin/assets/{name}")
        assert asset.status_code == 200
        assert asset.headers["content-type"].startswith(media_type)

    assert client.get("/admin/assets/admin.html").status_code == 404
    assert client.get("/admin/assets/qualquer.txt").status_code == 404


def test_the_panel_loads_nothing_from_another_origin() -> None:
    for name in ("admin.html", "admin.css", "admin.js"):
        text = (ASSETS_DIR / name).read_text(encoding="utf-8")
        assert "http://" not in text
        assert "https://" not in text


def test_the_panel_is_outside_the_public_schema(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert not [path for path in paths if path.startswith("/admin")]
    assert "/v1/health" in paths
