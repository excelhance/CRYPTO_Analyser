"""Cache incrémental des bougies (parquet), un fichier par `symbole_intervalle` (§2.5 CDC)."""
from __future__ import annotations

from pathlib import Path

import pandas as pd


def cache_file_path(cache_dir: str | Path, symbol: str, interval: str) -> Path:
    """Chemin du fichier parquet pour un couple (symbole, intervalle)."""
    return Path(cache_dir) / f"{symbol}_{interval}.parquet"


def load_klines(cache_dir: str | Path, symbol: str, interval: str) -> pd.DataFrame | None:
    """Charge les bougies en cache, ou `None` si aucun fichier n'existe encore."""
    path = cache_file_path(cache_dir, symbol, interval)
    if not path.is_file():
        return None
    return pd.read_parquet(path)


def save_klines(df: pd.DataFrame, cache_dir: str | Path, symbol: str, interval: str) -> None:
    """Persiste les bougies (écrase le fichier existant)."""
    path = cache_file_path(cache_dir, symbol, interval)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


def merge_tables(cached_df: pd.DataFrame | None, fresh_df: pd.DataFrame) -> pd.DataFrame:
    """Fusionne le cache et les données fraîches, sans dédoublonnage pyarrow.

    On ne garde du cache que les lignes antérieures à la plus ancienne bougie
    fraîche (`open_time < fresh_df.open_time.min()`), puis on concatène : les
    deux plages sont disjointes par construction, un dédoublonnage est donc
    inutile.

    Cas `fresh_df` vide (le NORME, pas l'exception, sur `1w`/`1M` : aucune
    nouvelle bougie clôturée depuis le dernier scan) : le cache est retourné
    **inchangé**. Ne jamais l'écraser avec un delta vide.
    """
    if cached_df is None or cached_df.empty:
        return fresh_df.reset_index(drop=True)
    if fresh_df.empty:
        return cached_df.reset_index(drop=True)
    min_fresh_open = fresh_df["open_time"].min()
    kept_from_cache = cached_df[cached_df["open_time"] < min_fresh_open]
    merged = pd.concat([kept_from_cache, fresh_df], ignore_index=True)
    return merged.sort_values("open_time").reset_index(drop=True)
