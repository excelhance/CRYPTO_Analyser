"""Tests de `data_fetcher` : univers */USDC, volumes 24h, bougies (via httpx.MockTransport).

Aucun appel réseau réel : le transport httpx est mocké. Le gouverneur utilise
le vrai `RateLimiter` (budgets larges, pas d'attente attendue dans ces tests).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from scanner import cache
from scanner.config import load_config
from scanner.data_fetcher import BASE_URL, DataFetcher, build_timeout
from scanner.rate_limiter import BinanceBannedError, RateLimiter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)

EXCHANGE_INFO_JSON = {
    "symbols": [
        {"symbol": "BTCUSDC", "quoteAsset": "USDC", "status": "TRADING", "isSpotTradingAllowed": True},
        {"symbol": "ETHUSDC", "quoteAsset": "USDC", "status": "TRADING", "isSpotTradingAllowed": True},
        {"symbol": "BTCUSDT", "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": True},  # mauvais quote
        {"symbol": "XUSDC", "quoteAsset": "USDC", "status": "BREAK", "isSpotTradingAllowed": True},  # pas TRADING
        {"symbol": "YUSDC", "quoteAsset": "USDC", "status": "TRADING", "isSpotTradingAllowed": False},  # spot interdit
    ]
}

TICKER_24H_JSON = [
    {"symbol": "BTCUSDC", "quoteVolume": "12345678.90"},
    {"symbol": "ETHUSDC", "quoteVolume": "999999.99"},
]


def _kline_row(open_time_ms: int, close_time_ms: int) -> list:
    return [
        open_time_ms, "100.0", "101.0", "99.0", "100.5", "10.0",
        close_time_ms, "1000.0", 5, "5.0", "500.0", "0",
    ]


KLINES_JSON = [
    _kline_row(NOW_MS - 20_000, NOW_MS - 10_000),  # clôturée
    _kline_row(NOW_MS - 10_000, NOW_MS - 5_000),   # clôturée
    _kline_row(NOW_MS - 5_000, NOW_MS + 5_000),    # en cours (close_time futur) => à exclure
]


def _load_cfg(tmp_path: Path):
    cfg = load_config(CONFIG_PATH)
    return cfg.model_copy(update={"cache": cfg.cache.model_copy(update={"directory": str(tmp_path)})})


@pytest.fixture
def call_counts() -> dict[str, int]:
    return {"exchangeInfo": 0, "ticker24hr": 0, "klines": 0}


@pytest.fixture
def fetcher(tmp_path, call_counts):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v3/exchangeInfo":
            call_counts["exchangeInfo"] += 1
            return httpx.Response(200, json=EXCHANGE_INFO_JSON, headers={"X-MBX-USED-WEIGHT-1M": "20"})
        if path == "/api/v3/ticker/24hr":
            call_counts["ticker24hr"] += 1
            return httpx.Response(200, json=TICKER_24H_JSON, headers={"X-MBX-USED-WEIGHT-1M": "100"})
        if path == "/api/v3/klines":
            call_counts["klines"] += 1
            return httpx.Response(200, json=KLINES_JSON, headers={"X-MBX-USED-WEIGHT-1M": "102"})
        raise AssertionError(f"URL inattendue : {request.url}")

    cfg = _load_cfg(tmp_path)
    rl = RateLimiter(
        budget_per_minute=cfg.rate_limiter.budget_per_minute,
        max_retries=cfg.rate_limiter.max_retries,
        backoff_base_seconds=cfg.rate_limiter.backoff_base_seconds,
    )
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    return DataFetcher(config=cfg, rate_limiter=rl, client=client, now_func=lambda: NOW)


def test_build_timeout_matches_config_without_raising():
    cfg = load_config(CONFIG_PATH)
    timeout = build_timeout(cfg.http)
    assert timeout.connect == cfg.http.connect_timeout
    assert timeout.read == cfg.http.read_timeout
    assert timeout.write == cfg.http.write_timeout
    assert timeout.pool == cfg.http.pool_timeout


def test_fetch_usdc_universe_filters_correctly(fetcher):
    universe = fetcher.fetch_usdc_universe()
    assert set(universe) == {"BTCUSDC", "ETHUSDC"}


def test_fetch_usdc_universe_called_once_per_session(fetcher, call_counts):
    fetcher.fetch_usdc_universe()
    fetcher.fetch_usdc_universe()
    assert call_counts["exchangeInfo"] == 1


def test_fetch_24h_quote_volumes_returns_floats(fetcher):
    volumes = fetcher.fetch_24h_quote_volumes()
    assert volumes["BTCUSDC"] == pytest.approx(12345678.90)


def test_fetch_24h_quote_volumes_called_once_per_session(fetcher, call_counts):
    fetcher.fetch_24h_quote_volumes()
    fetcher.fetch_24h_quote_volumes()
    assert call_counts["ticker24hr"] == 1


def test_get_klines_excludes_unclosed_candle(fetcher):
    df = fetcher.get_klines("BTCUSDC", "1d")
    assert len(df) == 2
    assert (df["close_time"] < NOW).all()


def test_get_klines_persists_to_cache(fetcher, tmp_path):
    fetcher.get_klines("BTCUSDC", "1d")
    assert cache.cache_file_path(tmp_path, "BTCUSDC", "1d").is_file()


def test_data_fetcher_propagates_banned_error_without_retry(tmp_path):
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(418, json={}, headers={})

    cfg = _load_cfg(tmp_path)
    rl = RateLimiter(
        budget_per_minute=cfg.rate_limiter.budget_per_minute,
        max_retries=cfg.rate_limiter.max_retries,
        backoff_base_seconds=cfg.rate_limiter.backoff_base_seconds,
    )
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    fetcher = DataFetcher(config=cfg, rate_limiter=rl, client=client, now_func=lambda: NOW)

    with pytest.raises(BinanceBannedError):
        fetcher.fetch_usdc_universe()
    assert call_count["n"] == 1  # aucun retry automatique derrière un 418
