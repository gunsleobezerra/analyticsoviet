"""Application-level dependency wiring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from core.cache import Cache
from core.config import Settings
from core.http import HttpClient
from db.session import Database
from platforms.base import AdapterContext
from platforms.registry import AdapterRegistry


@dataclass(slots=True)
class AppState:
    """Everything created once in the lifespan and shared across requests."""

    settings: Settings
    http: HttpClient
    cache: Cache
    registry: AdapterRegistry
    db: Database
    started_at: float


def build_state(settings: Settings, http: HttpClient, cache: Cache, db: Database) -> AppState:
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
    )


def get_state(request: Request) -> AppState:
    state = getattr(request.app.state, "app_state", None)
    if state is None:  # pragma: no cover - only if lifespan was skipped
        raise RuntimeError("application state not initialised")
    return state


StateDep = Annotated[AppState, Depends(get_state)]
