"""HTTP surface of the panel: login, session, logout and password change.

Every route here lives outside ``api_prefix`` and outside the public OpenAPI
document: the panel is not part of the published contract, and a client reading
``/openapi.json`` has no business discovering it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from api.deps import AdminDep, SessionDep, client_ip, enforce_csrf
from core.admin_auth import (
    COOKIE_PATH,
    CSRF_COOKIE,
    SESSION_COOKIE,
    AdminApiError,
    AdminSessionView,
    LoginRefused,
    PasswordChangeRefused,
)

#: One message for every failed login, whatever the real cause was.
LOGIN_REFUSED_MESSAGE = "invalid username or password"

router = APIRouter()

#: Everything that requires a session, and a CSRF token on writes.
protected = APIRouter(dependencies=[Depends(enforce_csrf)])


async def admin_error_handler(_: Request, exc: AdminApiError) -> JSONResponse:
    """Render an admin failure in the same envelope every other error uses."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


def _session_payload(session: AdminSessionView, csrf_token: str | None) -> dict[str, Any]:
    return {
        "username": session.username,
        "must_change_password": session.must_change_password,
        "expires_at": session.expires_at.isoformat(),
        "csrf_token": csrf_token,
    }


def _set_session_cookies(
    response: JSONResponse,
    request: Request,
    *,
    session_id: str,
    csrf_token: str,
    max_age: int,
) -> None:
    """Attach both cookies.

    ``Secure`` follows the environment rather than the request: production is
    always served over TLS, and marking the cookie Secure there keeps a stray
    plain-http request from carrying it. Development over http://localhost would
    lose the cookie entirely if it were always on.
    """
    secure = request.app.state.settings.environment == "prod"
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=max_age,
        path=COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=secure,
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token,
        max_age=max_age,
        path=COOKIE_PATH,
        httponly=False,
        samesite="lax",
        secure=secure,
    )


def _clear_session_cookies(response: JSONResponse, request: Request) -> None:
    secure = request.app.state.settings.environment == "prod"
    response.delete_cookie(SESSION_COOKIE, path=COOKIE_PATH, secure=secure, httponly=True)
    response.delete_cookie(CSRF_COOKIE, path=COOKIE_PATH, secure=secure)


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise AdminApiError(400, "invalid_request", "body must be JSON") from exc
    if not isinstance(payload, dict):
        raise AdminApiError(400, "invalid_request", "body must be a JSON object")
    return payload


def _text(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    return value if isinstance(value, str) else ""


@router.post("/admin/api/login", include_in_schema=False)
async def login(request: Request, service: AdminDep) -> JSONResponse:
    """Exchange credentials for a session cookie and a CSRF token.

    This is the one write that cannot carry a CSRF token: the token does not
    exist until the session it belongs to does.
    """
    payload = await _json_body(request)
    username = _text(payload, "username").strip()
    password = _text(payload, "password")
    if not username or not password:
        raise AdminApiError(400, "invalid_request", "username and password are required")

    try:
        session, csrf_token = await service.login(
            username=username,
            password=password,
            ip=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            forwarded_proto=request.headers.get("x-forwarded-proto"),
        )
    except LoginRefused as exc:
        if exc.reason == "insecure_transport":
            raise AdminApiError(
                403, "https_required", "the panel only accepts logins over https"
            ) from exc
        raise AdminApiError(exc.status_code, "invalid_credentials", LOGIN_REFUSED_MESSAGE) from exc

    response = JSONResponse(_session_payload(session, csrf_token))
    _set_session_cookies(
        response,
        request,
        session_id=session.session_id,
        csrf_token=csrf_token,
        max_age=service.session_policy.absolute_seconds,
    )
    return response


@protected.get("/admin/api/session", include_in_schema=False)
async def read_session(request: Request, session: SessionDep) -> dict[str, Any]:
    """Who the caller is, and whether the initial password is still in place."""
    return _session_payload(session, request.cookies.get(CSRF_COOKIE))


@protected.post("/admin/api/logout", include_in_schema=False)
async def logout(request: Request, session: SessionDep, service: AdminDep) -> JSONResponse:
    await service.logout(session)
    response = JSONResponse({"logged_out": True})
    _clear_session_cookies(response, request)
    return response


@protected.post("/admin/api/password", include_in_schema=False)
async def change_password(request: Request, session: SessionDep, service: AdminDep) -> JSONResponse:
    """Replace the password and clear the pending-change flag.

    Every session of the user dies with it, the current one included: the
    response asks for a new login, and the panel sends the operator to the form.
    """
    payload = await _json_body(request)
    current_password = _text(payload, "current_password")
    new_password = _text(payload, "new_password")
    if not current_password or not new_password:
        raise AdminApiError(400, "invalid_request", "current_password and new_password are required")

    try:
        await service.change_password(
            session,
            current_password=current_password,
            new_password=new_password,
        )
    except PasswordChangeRefused as exc:
        code = "invalid_credentials" if exc.status_code == 401 else "password_rejected"
        raise AdminApiError(exc.status_code, code, str(exc)) from exc
    except LoginRefused as exc:
        raise AdminApiError(exc.status_code, "invalid_credentials", LOGIN_REFUSED_MESSAGE) from exc

    response = JSONResponse({"changed": True, "relogin_required": True})
    _clear_session_cookies(response, request)
    return response
