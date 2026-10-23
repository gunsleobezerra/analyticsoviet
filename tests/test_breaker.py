"""Tests for the circuit breaker implementation."""

from __future__ import annotations

import time

from core.breaker import CircuitBreaker


def test_breaker_disabled_by_default() -> None:
    breaker = CircuitBreaker()
    assert not breaker.enabled
    for _ in range(10):
        breaker.record_failure("instagram")
    assert breaker.allow("instagram")
    assert breaker.state("instagram") == "closed"
    assert breaker.snapshot() == {}


def test_breaker_enabled_trips_and_cooldown() -> None:
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=1, enabled=True)
    assert breaker.enabled
    assert breaker.allow("instagram")

    breaker.record_failure("instagram")
    assert breaker.allow("instagram")
    assert breaker.state("instagram") == "closed"

    breaker.record_failure("instagram")
    assert not breaker.allow("instagram")
    assert breaker.state("instagram") == "open"
    assert breaker.snapshot() == {"instagram": "open"}

    # Simulate waiting past cooldown
    breaker._opened_at["instagram"] = time.monotonic() - 2.0
    # Next allow transitions to half-open probe
    assert breaker.allow("instagram")
    assert breaker.state("instagram") == "half_open"

    # Success closes the breaker
    breaker.record_success("instagram")
    assert breaker.state("instagram") == "closed"
    assert breaker.allow("instagram")
