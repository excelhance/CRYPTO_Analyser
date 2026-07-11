"""Tests de l'orchestrateur (`scanner.py`, Lot 4) : gate, robustesse, débit, tri."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from scanner.config import load_config
from scanner.data_fetcher import BASE_URL, DataFetcher
from scanner.rate_limiter import BinanceBannedError, RateLimiter
from scanner.scanner import run_scan

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"

NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
INTERVAL_MS = {
    "4h": 4 * 3600_000, "12h": 12 * 3600_000, "1d": 86_400_000,
    "1w": 7 * 86_400_000, "1M": 30 * 86_400_000,
}


def _kline_row(open_ms: int, close_ms: int, price: float) -> list:
    return [
        open_ms, f"{price}", f"{price + 1}", f"{price - 1}", f"{price}", "10.0",
        close_ms, "1000.0", 5, "5.0", "500.0", "0",
    ]


def _klines_json(interval: str, n: int) -> list:
    step = INTERVAL_MS[interval]
    rows = []
    for i in range(n):
        open_ms = NOW_MS - (n - i) * step
        close_ms = open_ms + step - 1
        rows.append(_kline_row(open_ms, close_ms, 100.0 + i))
    return rows


def _make_handler(symbols_info: list[dict], volumes: dict[str, str], behavior: dict[str, str], call_log: list):
    """`behavior` : "normal" (10 bougies), "short" (2, sous le gate relâché),
    "corrupt" (JSON invalide), "banned" (418). Défaut "normal" si absent."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v3/exchangeInfo":
            return httpx.Response(200, json={"symbols": symbols_info}, headers={"X-MBX-USED-WEIGHT-1M": "20"})
        if path == "/api/v3/ticker/24hr":
            tickers = [{"symbol": s, "quoteVolume": v} for s, v in volumes.items()]
            return httpx.Response(200, json=tickers, headers={"X-MBX-USED-WEIGHT-1M": "100"})
        if path == "/api/v3/klines":
            symbol = request.url.params["symbol"]
            interval = request.url.params["interval"]
            call_log.append((symbol, interval))
            mode = behavior.get(symbol, "normal")
            if mode == "banned":
                return httpx.Response(418, json={}, headers={})
            if mode == "corrupt":
                return httpx.Response(200, json=[["bad"]], headers={"X-MBX-USED-WEIGHT-1M": "2"})
            n = 2 if mode == "short" else 10
            return httpx.Response(200, json=_klines_json(interval, n), headers={"X-MBX-USED-WEIGHT-1M": "2"})
        raise AssertionError(f"URL inattendue : {request.url}")

    return handler


def _relaxed_config(tmp_path: Path):
    cfg = load_config(CONFIG_PATH)
    cfg = cfg.model_copy(update={"gates": cfg.gates.model_copy(update={"min_bars_per_tf": 3})})
    cfg = cfg.model_copy(update={"cache": cfg.cache.model_copy(update={"directory": str(tmp_path)})})
    return cfg


def _fetcher(cfg, handler, rate_limiter=None) -> tuple[DataFetcher, RateLimiter]:
    rl = rate_limiter or RateLimiter(
        cfg.rate_limiter.budget_per_minute, cfg.rate_limiter.max_retries, cfg.rate_limiter.backoff_base_seconds
    )
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    return DataFetcher(config=cfg, rate_limiter=rl, client=client, now_func=lambda: NOW), rl


def _symbol_info(symbol: str, quote_asset: str = "USDC") -> dict:
    return {"symbol": symbol, "quoteAsset": quote_asset, "status": "TRADING", "isSpotTradingAllowed": True}


def test_gate_filters_correctly_and_never_calls_klines_for_excluded_symbols(tmp_path):
    cfg = _relaxed_config(tmp_path)
    gate = cfg.gates.min_quote_volume_24h
    symbols_info = [
        _symbol_info("AUSDC"), _symbol_info("BUSDC"),
        _symbol_info("FUSDC"),  # sous le gate
        _symbol_info("GUSDT", quote_asset="USDT"),  # mauvais quote asset
    ]
    volumes = {
        "AUSDC": str(gate * 4), "BUSDC": str(gate * 4),
        "FUSDC": str(gate * 0.1), "GUSDT": str(gate * 10),
    }
    call_log: list = []
    handler = _make_handler(symbols_info, volumes, behavior={}, call_log=call_log)
    fetcher, _rl = _fetcher(cfg, handler)

    result = run_scan(cfg, fetcher=fetcher)

    assert result.summary.universe_size == 3  # GUSDT exclu au niveau de l'univers (mauvais quote asset)
    assert result.summary.qualifying_count == 2  # AUSDC, BUSDC (FUSDC sous le gate)
    called_symbols = {symbol for symbol, _interval in call_log}
    assert called_symbols == {"AUSDC", "BUSDC"}  # FUSDC/GUSDT jamais interrogés


def test_one_pair_error_does_not_abort_the_scan(tmp_path):
    cfg = _relaxed_config(tmp_path)
    gate = cfg.gates.min_quote_volume_24h
    symbols_info = [_symbol_info("AUSDC"), _symbol_info("EUSDC")]
    volumes = {"AUSDC": str(gate * 4), "EUSDC": str(gate * 4)}
    call_log: list = []
    handler = _make_handler(symbols_info, volumes, behavior={"EUSDC": "corrupt"}, call_log=call_log)
    fetcher, _rl = _fetcher(cfg, handler)

    result = run_scan(cfg, fetcher=fetcher)

    assert "EUSDC" in result.summary.failed_symbols
    assert result.summary.scored_count == 1
    assert result.rows[0].symbol == "AUSDC"


def test_excluded_pair_not_in_rows_but_counted_in_summary(tmp_path):
    cfg = _relaxed_config(tmp_path)
    gate = cfg.gates.min_quote_volume_24h
    symbols_info = [_symbol_info("AUSDC"), _symbol_info("BUSDC")]
    volumes = {"AUSDC": str(gate * 4), "BUSDC": str(gate * 4)}
    call_log: list = []
    # BUSDC : historique trop court (2 < gates.min_bars_per_tf=3) sur tous les TF => déclenchement vide => exclue.
    handler = _make_handler(symbols_info, volumes, behavior={"BUSDC": "short"}, call_log=call_log)
    fetcher, _rl = _fetcher(cfg, handler)

    result = run_scan(cfg, fetcher=fetcher)

    assert result.summary.excluded_count == 1
    assert all(row.symbol != "BUSDC" for row in result.rows)
    assert result.summary.scored_count == 1


def test_rows_sorted_by_score_descending(tmp_path):
    cfg = _relaxed_config(tmp_path)
    gate = cfg.gates.min_quote_volume_24h
    symbols_info = [_symbol_info(s) for s in ("AUSDC", "BUSDC", "CUSDC")]
    volumes = {s: str(gate * 4) for s in ("AUSDC", "BUSDC", "CUSDC")}
    call_log: list = []
    handler = _make_handler(symbols_info, volumes, behavior={}, call_log=call_log)
    fetcher, _rl = _fetcher(cfg, handler)

    result = run_scan(cfg, fetcher=fetcher)

    scores = [row.result.score for row in result.rows]
    assert scores == sorted(scores, reverse=True)


def test_banned_ip_aborts_entire_scan_without_processing_remaining_symbols(tmp_path):
    cfg = _relaxed_config(tmp_path)
    gate = cfg.gates.min_quote_volume_24h
    # Ordre alphabétique du gate (list comprehension sur l'univers) : AUSDC traité avant ZUSDC.
    symbols_info = [_symbol_info("AUSDC"), _symbol_info("ZUSDC")]
    volumes = {"AUSDC": str(gate * 4), "ZUSDC": str(gate * 4)}
    call_log: list = []
    handler = _make_handler(symbols_info, volumes, behavior={"AUSDC": "banned"}, call_log=call_log)
    fetcher, _rl = _fetcher(cfg, handler)

    with pytest.raises(BinanceBannedError):
        run_scan(cfg, fetcher=fetcher)

    called_symbols = {symbol for symbol, _interval in call_log}
    assert "ZUSDC" not in called_symbols  # jamais atteint : le scan s'est arrêté sur le ban


def test_total_weight_consumed_is_per_scan_not_cumulative_across_two_scans(tmp_path):
    cfg = _relaxed_config(tmp_path)
    gate = cfg.gates.min_quote_volume_24h
    symbols_info = [_symbol_info("AUSDC"), _symbol_info("BUSDC")]
    volumes = {"AUSDC": str(gate * 4), "BUSDC": str(gate * 4)}

    shared_rate_limiter = RateLimiter(
        cfg.rate_limiter.budget_per_minute, cfg.rate_limiter.max_retries, cfg.rate_limiter.backoff_base_seconds
    )
    call_log_1: list = []
    fetcher_1, _ = _fetcher(cfg, _make_handler(symbols_info, volumes, {}, call_log_1), rate_limiter=shared_rate_limiter)
    call_log_2: list = []
    fetcher_2, _ = _fetcher(cfg, _make_handler(symbols_info, volumes, {}, call_log_2), rate_limiter=shared_rate_limiter)

    result_1 = run_scan(cfg, fetcher=fetcher_1, rate_limiter=shared_rate_limiter)
    result_2 = run_scan(cfg, fetcher=fetcher_2, rate_limiter=shared_rate_limiter)

    expected_weight = 20 + 80 + 2 * len(cfg.intervals) * 2  # exchangeInfo + ticker24hr + 2 paires x 5 TF x poids klines
    assert result_1.summary.total_weight_consumed == expected_weight
    assert result_2.summary.total_weight_consumed == expected_weight  # pas le double : delta, pas cumul
    assert shared_rate_limiter.total_consumed == expected_weight * 2  # le cumul brut, lui, double bien
