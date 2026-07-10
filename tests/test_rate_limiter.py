"""Tests du gouverneur de débit (`rate_limiter`).

Horloge et sleep factices : aucune attente réelle pendant les tests.
"""
from __future__ import annotations

import pytest

from scanner.rate_limiter import BinanceBannedError, RateLimiter, WINDOW_SECONDS


class FakeClock:
    """Horloge manuelle : `sleep` avance le temps sans attente réelle."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _rate_limiter(budget: int, max_retries: int = 3, backoff: float = 1.0) -> tuple[RateLimiter, FakeClock]:
    clock = FakeClock()
    rl = RateLimiter(
        budget_per_minute=budget,
        max_retries=max_retries,
        backoff_base_seconds=backoff,
        time_func=clock.time,
        sleep_func=clock.sleep,
    )
    return rl, clock


def test_acquire_does_not_block_below_budget():
    rl, clock = _rate_limiter(budget=100)
    rl.acquire(20)
    rl.acquire(20)
    assert clock.sleeps == []


def test_acquire_blocks_when_budget_would_be_exceeded():
    rl, clock = _rate_limiter(budget=10)
    rl.acquire(6)
    rl.acquire(6)  # 6+6 > 10 : doit attendre l'expiration du premier évènement
    assert clock.sleeps, "une attente aurait dû se produire"
    assert clock.now >= WINDOW_SECONDS


def test_current_usage_reflects_acquired_weight():
    rl, _clock = _rate_limiter(budget=100)
    rl.acquire(20)
    rl.acquire(15)
    assert rl.current_usage() == 35


def test_sync_from_headers_aligns_on_authoritative_usage():
    rl, clock = _rate_limiter(budget=10)
    rl.acquire(2)
    rl.sync_from_headers({"X-MBX-USED-WEIGHT-1M": "9"})  # Binance rapporte 9 déjà consommés
    rl.acquire(2)  # 9 + 2 > 10 : doit désormais attendre
    assert clock.sleeps


def test_sync_from_headers_ignores_unreadable_value():
    rl, clock = _rate_limiter(budget=10)
    rl.sync_from_headers({"X-MBX-USED-WEIGHT-1M": "pas-un-nombre"})
    rl.acquire(10)  # ne doit pas lever, la valeur illisible est simplement ignorée
    assert clock.sleeps == []


def test_handle_429_uses_retry_after_header():
    rl, clock = _rate_limiter(budget=100)
    rl.handle_error_response(429, {"Retry-After": "5"}, attempt=0)
    assert clock.sleeps == [5.0]


def test_handle_429_exponential_backoff_without_retry_after():
    rl, clock = _rate_limiter(budget=100, backoff=1.0)
    rl.handle_error_response(429, {}, attempt=2)
    assert clock.sleeps == [4.0]  # 1.0 * 2**2


def test_handle_429_raises_after_max_retries_exhausted():
    rl, _clock = _rate_limiter(budget=100, max_retries=2)
    with pytest.raises(RuntimeError):
        rl.handle_error_response(429, {}, attempt=2)


def test_handle_418_raises_banned_error_without_retry():
    rl, clock = _rate_limiter(budget=100)
    with pytest.raises(BinanceBannedError):
        rl.handle_error_response(418, {}, attempt=0)
    assert clock.sleeps == []  # aucun retry/backoff automatique sur un ban
