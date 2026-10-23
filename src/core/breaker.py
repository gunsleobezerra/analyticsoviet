"""Circuit breaker used to stop hammering a platform that is blocking us."""

from __future__ import annotations

import time


class CircuitBreaker:
    """Classic three-state breaker (closed / open / half-open)."""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        cooldown_seconds: int = 10,
        enabled: bool = False,
    ) -> None:
        self.enabled = enabled
        self.failure_threshold = max(failure_threshold, 1)
        self.cooldown_seconds = max(cooldown_seconds, 1)
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self._half_open: set[str] = set()

    def allow(self, key: str) -> bool:
        """Return True when a request may proceed."""
        if not self.enabled:
            return True
        opened_at = self._opened_at.get(key)
        if opened_at is None:
            return True
        if time.monotonic() - opened_at >= self.cooldown_seconds:
            # transition to half-open: allow a single probe request
            self._half_open.add(key)
            self._opened_at.pop(key, None)
            return True
        return False

    def record_success(self, key: str) -> None:
        if not self.enabled:
            return
        self._failures.pop(key, None)
        self._opened_at.pop(key, None)
        self._half_open.discard(key)

    def record_failure(self, key: str) -> None:
        if not self.enabled:
            return
        if key in self._half_open:
            # probe failed -> re-open immediately
            self._half_open.discard(key)
            self._opened_at[key] = time.monotonic()
            return
        failures = self._failures.get(key, 0) + 1
        self._failures[key] = failures
        if failures >= self.failure_threshold:
            self._opened_at[key] = time.monotonic()

    def state(self, key: str) -> str:
        if not self.enabled:
            return "closed"
        if key in self._half_open:
            return "half_open"
        if key in self._opened_at:
            return "open"
        return "closed"

    def snapshot(self) -> dict[str, str]:
        if not self.enabled:
            return {}
        keys = set(self._failures) | set(self._opened_at) | self._half_open
        return {key: self.state(key) for key in sorted(keys)}

    def reset(self) -> None:
        self._failures.clear()
        self._opened_at.clear()
        self._half_open.clear()
