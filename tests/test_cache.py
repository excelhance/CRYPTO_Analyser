"""Tests du cache parquet incrémental (`cache.py`)."""
from __future__ import annotations

import pandas as pd
import pytest

from scanner import cache


def _df(open_times_ms: list[int]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open_time": pd.to_datetime(open_times_ms, unit="ms", utc=True),
            "close": [1.0] * len(open_times_ms),
        }
    )


def test_merge_tables_no_cache_returns_fresh_data():
    fresh = _df([0, 60_000])
    merged = cache.merge_tables(None, fresh)
    pd.testing.assert_frame_equal(merged, fresh.reset_index(drop=True))


def test_merge_tables_fresh_empty_returns_cache_unchanged():
    """Cas NORMAL sur 1w/1M : pas de nouvelle bougie close => cache inchangé, pas écrasé."""
    cached = _df([0, 60_000])
    fresh = pd.DataFrame(columns=["open_time", "close"])
    merged = cache.merge_tables(cached, fresh)
    pd.testing.assert_frame_equal(merged.reset_index(drop=True), cached.reset_index(drop=True))


def test_merge_tables_concatenates_disjoint_ranges_sorted():
    cached = _df([0, 60_000])
    fresh = _df([120_000, 180_000])
    merged = cache.merge_tables(cached, fresh)
    assert list(merged["open_time"]) == list(_df([0, 60_000, 120_000, 180_000])["open_time"])


def test_merge_tables_drops_cached_rows_at_or_after_fresh_min_open_time():
    """Pas de dédoublonnage pyarrow : on filtre le cache sur `open_time < min(fresh)`."""
    cached = _df([0, 60_000, 120_000])  # 120_000 est aussi présent côté fresh
    fresh = _df([120_000, 180_000])
    merged = cache.merge_tables(cached, fresh)
    assert len(merged) == 4  # 0, 60_000 (cache) + 120_000, 180_000 (fresh) ; pas de doublon 120_000
    assert list(merged["open_time"]) == list(_df([0, 60_000, 120_000, 180_000])["open_time"])


def test_save_and_load_klines_roundtrip(tmp_path):
    df = _df([0, 60_000])
    cache.save_klines(df, tmp_path, "BTCUSDC", "1d")
    loaded = cache.load_klines(tmp_path, "BTCUSDC", "1d")
    pd.testing.assert_frame_equal(loaded, df)


def test_load_klines_missing_file_returns_none(tmp_path):
    assert cache.load_klines(tmp_path, "BTCUSDC", "1d") is None
