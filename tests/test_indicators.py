"""Tests des indicateurs techniques (`indicators.py`, §3 CDC)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scanner.config import load_config
from scanner.indicators import CANDLESTICK_PATTERNS, compute_indicators

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"

CANDLESTICK_COLUMNS = [label for _talib_name, label in CANDLESTICK_PATTERNS]

CONTINUOUS_LOCKED_COLUMNS = [
    "ema_20", "ema_50", "ema_200", "rsi", "macd", "macd_signal", "macd_hist",
    "sar", "bb_upper", "bb_middle", "bb_lower", "bb_width", "percent_b",
    "mom", "volume_sma", "adx", "plus_di", "minus_di", "atr", "atr_pct",
]


def _synthetic_ohlcv(n: int, seed: int = 42, trend: float = 0.1) -> pd.DataFrame:
    """Série OHLCV synthétique déterministe (marche aléatoire avec dérive)."""
    rng = np.random.default_rng(seed)
    open_time = pd.date_range("2020-01-01", periods=n, freq="D", tz="UTC")
    close = 100 + np.cumsum(rng.normal(loc=trend, scale=1.0, size=n))
    close = np.maximum(close, 1.0)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = np.maximum(open_, close) + rng.uniform(0, 1, size=n)
    low = np.maximum(np.minimum(open_, close) - rng.uniform(0, 1, size=n), 0.01)
    volume = rng.uniform(10, 100, size=n)
    close_time = open_time + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)
    return pd.DataFrame(
        {
            "open_time": open_time,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "close_time": close_time,
        }
    )


@pytest.fixture
def cfg():
    return load_config(CONFIG_PATH)


def test_full_history_all_locked_indicators_present(cfg):
    df = _synthetic_ohlcv(300)
    result = compute_indicators(df, cfg.indicators, cfg.gates)

    assert result.omitted == []
    expected = set(CONTINUOUS_LOCKED_COLUMNS) | set(CANDLESTICK_COLUMNS) | {"ema_aligned_bullish"}
    assert expected.issubset(result.data.columns)

    last = result.data.iloc[-1]
    for col in CONTINUOUS_LOCKED_COLUMNS:
        assert pd.notna(last[col]), f"{col} ne devrait pas être NaN sur la dernière ligne"
    assert last["ema_aligned_bullish"] in (True, False, np.True_, np.False_)


def test_short_history_omits_long_period_indicators(cfg):
    """Simule une paire jeune (1W/1M) : seuls SAR et les CDL* survivent."""
    df = _synthetic_ohlcv(5)
    result = compute_indicators(df, cfg.indicators, cfg.gates)

    expected_omitted = {
        "ema_20", "ema_50", "ema_200", "rsi", "macd", "bbands",
        "mom", "volume_sma", "adx", "atr", "ema_aligned_bullish",
    }
    assert set(result.omitted) == expected_omitted

    # Jamais de 0 en lieu et place d'un indicateur omis : la colonne est absente.
    for label in ("ema_20", "ema_50", "ema_200", "rsi", "macd", "mom", "volume_sma", "adx", "atr"):
        assert label not in result.data.columns

    assert "sar" in result.data.columns
    for col in CANDLESTICK_COLUMNS:
        assert col in result.data.columns


def test_ema200_gated_explicitly_below_ema200_min_bars(cfg):
    """EMA longue : garde dédiée sur gates.ema200_min_bars, pas seulement sur le NaN TA-Lib."""
    df = _synthetic_ohlcv(cfg.gates.ema200_min_bars - 1)
    result = compute_indicators(df, cfg.indicators, cfg.gates)
    assert "ema_200" in result.omitted
    assert "ema_200" not in result.data.columns


def test_warmup_nan_preserved_not_dropped(cfg):
    """Les NaN de chauffe restent dans la colonne (pas de suppression de lignes)."""
    df = _synthetic_ohlcv(60)
    result = compute_indicators(df, cfg.indicators, cfg.gates)

    assert len(result.data) == 60
    ema20 = result.data["ema_20"]
    assert ema20.iloc[:19].isna().all()  # lookback EMA(20) = 19
    assert ema20.iloc[19:].notna().all()


def test_derived_columns_are_consistent(cfg):
    df = _synthetic_ohlcv(300)
    result = compute_indicators(df, cfg.indicators, cfg.gates)
    data = result.data

    last = data.iloc[-1]
    assert last["atr_pct"] == pytest.approx(last["atr"] / last["close"])
    assert last["bb_width"] == pytest.approx((last["bb_upper"] - last["bb_lower"]) / last["bb_middle"])
    assert last["percent_b"] == pytest.approx((last["close"] - last["bb_lower"]) / (last["bb_upper"] - last["bb_lower"]))


def test_compute_indicators_sorts_input_by_open_time(cfg):
    df = _synthetic_ohlcv(300)
    shuffled = df.sample(frac=1.0, random_state=7).reset_index(drop=True)

    result_sorted = compute_indicators(df, cfg.indicators, cfg.gates)
    result_shuffled = compute_indicators(shuffled, cfg.indicators, cfg.gates)

    assert result_shuffled.data["open_time"].is_monotonic_increasing
    pd.testing.assert_series_equal(
        result_sorted.data["rsi"].reset_index(drop=True),
        result_shuffled.data["rsi"].reset_index(drop=True),
    )
