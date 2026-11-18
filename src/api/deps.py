"""Application-level dependency wiring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from core.admin_auth import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    AdminApiError,
    AdminAuthService,
    AdminSessionView,
    CsrfRefused,
)
from core.cache import Cache
from core.config import Settings
from core.config_store import ConfigStore
from core.http import HttpClient
from db.session import Database
from platforms.base import AdapterContext
from platforms.registry import AdapterRegistry

#: Methods that never change state, and so never need a CSRF token.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(slots=True)
class AppState:
    """Everything created once in the lifespan and shared across requests."""

    settings: Settings
    http: HttpClient
    cache: Cache
    registry: AdapterRegistry
    db: Database
    started_at: float
    config: ConfigStore | None = None
    admin: AdminAuthService | None = None


def build_state(
    settings: Settings,
    http: HttpClient,
    cache: Cache,
    db: Database,
    config: ConfigStore | None = None,
    admin: AdminAuthService | None = None,
) -> AppState:
    import time

    from core.logging import get_logger

    context = AdapterContext(
        settings=settings,
        http=http,
        cache=cache,
        logger=get_logger("adapter"),
    )
    return AppState(
        settings=settings,
        http=http,
        cache=cache,
        registry=AdapterRegistry.default(context),
        db=db,
        started_at=time.monotonic(),
        config=config,
        admin=admin,
    )


def get_state(request: Request) -> AppState:
    state = getattr(request.app.state, "app_state", None)
    if state is None:  # pragma: no cover - only if lifespan was skipped
        raise RuntimeError("application state not initialised")
    return state


StateDep = Annotated[AppState, Depends(get_state)]


def get_admin_auth(state: StateDep) -> AdminAuthService:
    service = state.admin
    if service is None:  # pragma: no cover - only if lifespan was skipped
        raise RuntimeError("admin authentication not initialised")
    return service


AdminDep = Annotated[AdminAuthService, Depends(get_admin_auth)]


def client_ip(request: Request) -> str:
    """The address a login attempt is throttled against.

    Behind the reverse proxy the socket peer is the proxy itself, so the
    forwarded chain is the only usable value; outside production that header is
    trivially forged and would let a client pick its own bucket, so the peer wins.
    """
    if request.app.state.settings.environment == "prod":
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client is not None else "unknown"


async def current_session(request: Request, service: AdminDep) -> AdminSessionView:
    """The authenticated operator behind this request, or a 401."""
    session = await service.open_session(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise AdminApiError(401, "unauthenticated", "sign in to use the panel")
    return session


SessionDep = Annotated[AdminSessionView, Depends(current_session)]


async def enforce_csrf(request: Request, service: AdminDep, session: SessionDep) -> None:
    """Reject a state-changing request that does not prove it came from the panel.

    A dependency rather than a middleware so it runs with the session it needs
    and only on the router that carries the panel's writes.
    """
    if request.method in SAFE_METHODS:
        return
    try:
        service.check_csrf(
            session,
            header_token=request.headers.get(CSRF_HEADER),
            cookie_token=request.cookies.get(CSRF_COOKIE),
        )
    except CsrfRefused as exc:
        await service.record_csrf_failure(
            username=session.username,
            ip=client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        raise AdminApiError(403, "csrf_failed", "missing or invalid CSRF token") from exc


async def require_password_changed(session: SessionDep) -> AdminSessionView:
    """Gate for configuration writes while the initial password is still in use.

    Reusable on purpose: the write routes attach it as a dependency, so the rule
    lives in one place and no new route can forget it. The only way to clear the
    flag is the password change the panel offers.
    """
    if session.must_change_password:
        raise AdminApiError(
            422,
            "password_change_required",
            "change the initial password before writing configuration",
        )
    return session
