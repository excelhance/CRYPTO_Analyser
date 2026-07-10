"""Récupération des données publiques Binance Spot (§2 CDC).

Trois familles d'appels, tous passés par le gouverneur (`rate_limiter`) :
- `exchangeInfo` : univers `*/USDC` (poids ~20, un seul appel par session).
- `ticker/24hr` (sans paramètre) : volumes 24h pour le gate de liquidité
  (poids 80, un seul appel par session).
- `klines` : bougies OHLCV par (symbole, intervalle) (poids 2), avec cache
  incrémental et exclusion systématique de la bougie en cours.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone

import httpx
import pandas as pd

from . import cache
from .config import AppConfig, HttpCfg
from .constants import WEIGHT_EXCHANGE_INFO, WEIGHT_KLINES, WEIGHT_TICKER_24H_ALL
from .rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

BASE_URL = "https://api.binance.com"

# Ordre des 11 premiers champs d'une bougie klines (le 12e, "ignore", est écarté).
KLINES_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "nb_trades",
    "taker_buy_base", "taker_buy_quote",
]

_FLOAT_COLUMNS = (
    "open", "high", "low", "close", "volume",
    "quote_volume", "taker_buy_base", "taker_buy_quote",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def build_timeout(http_cfg: HttpCfg) -> httpx.Timeout:
    """Timeout httpx explicite (connexion + lecture + écriture + pool), depuis la config.

    `httpx.Timeout` exige soit une valeur par défaut, soit les quatre
    paramètres (`connect`, `read`, `write`, `pool`) explicitement renseignés —
    on les fournit tous les quatre pour éviter toute ambiguïté.
    """
    return httpx.Timeout(
        http_cfg.read_timeout,
        connect=http_cfg.connect_timeout,
        read=http_cfg.read_timeout,
        write=http_cfg.write_timeout,
        pool=http_cfg.pool_timeout,
    )


class DataFetcher:
    """Point d'accès unique à l'API publique Binance Spot pour une session de scan.

    Met en cache en mémoire l'univers (`exchangeInfo`) et les volumes 24h
    (`ticker/24hr`) : un seul appel par instance, ces deux endpoints étant les
    plus coûteux en poids (§2.1/§2.4 CDC).
    """

    def __init__(
        self,
        config: AppConfig,
        rate_limiter: RateLimiter,
        client: httpx.Client | None = None,
        now_func: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._config = config
        self._rate_limiter = rate_limiter
        self._client = client or httpx.Client(base_url=BASE_URL, timeout=build_timeout(config.http))
        self._now = now_func
        self._exchange_info: dict | None = None
        self._tickers_24h: dict[str, float] | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DataFetcher":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Requête générique : gouverneur + retry borné sur 429                 #
    # ------------------------------------------------------------------ #
    def _get(self, path: str, params: dict[str, object], weight: int) -> httpx.Response:
        attempt = 0
        while True:
            self._rate_limiter.acquire(weight)
            response = self._client.get(path, params=params)
            self._rate_limiter.sync_from_headers(response.headers)
            if response.status_code in (418, 429):
                self._rate_limiter.handle_error_response(response.status_code, response.headers, attempt)
                attempt += 1
                continue
            response.raise_for_status()
            return response

    # ------------------------------------------------------------------ #
    # Univers */USDC (§2.1)                                                #
    # ------------------------------------------------------------------ #
    def fetch_usdc_universe(self) -> list[str]:
        """Symboles `*/USDC` actifs. Un seul appel `exchangeInfo` par instance."""
        if self._exchange_info is None:
            response = self._get("/api/v3/exchangeInfo", params={}, weight=WEIGHT_EXCHANGE_INFO)
            self._exchange_info = response.json()
            logger.info("exchangeInfo récupéré : %d symboles au total", len(self._exchange_info.get("symbols", [])))

        quote_asset = self._config.universe.quote_asset
        status = self._config.universe.status
        return [
            s["symbol"]
            for s in self._exchange_info.get("symbols", [])
            if s.get("quoteAsset") == quote_asset
            and s.get("status") == status
            and s.get("isSpotTradingAllowed") is True
        ]

    # ------------------------------------------------------------------ #
    # Volumes 24h — gate de liquidité (§4.5)                               #
    # ------------------------------------------------------------------ #
    def fetch_24h_quote_volumes(self) -> dict[str, float]:
        """`{symbole: quoteVolume 24h}`. Un seul appel `ticker/24hr` (poids 80) par instance."""
        if self._tickers_24h is None:
            response = self._get("/api/v3/ticker/24hr", params={}, weight=WEIGHT_TICKER_24H_ALL)
            data = response.json()
            self._tickers_24h = {item["symbol"]: float(item["quoteVolume"]) for item in data}
            logger.info("ticker/24hr récupéré : %d symboles", len(self._tickers_24h))
        return self._tickers_24h

    # ------------------------------------------------------------------ #
    # Bougies OHLCV (§2.2)                                                 #
    # ------------------------------------------------------------------ #
    def _fetch_klines_raw(
        self, symbol: str, interval: str, limit: int, start_time_ms: int | None
    ) -> pd.DataFrame:
        params: dict[str, object] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        response = self._get("/api/v3/klines", params=params, weight=WEIGHT_KLINES)
        raw = response.json()
        if not raw:
            return pd.DataFrame(columns=KLINES_COLUMNS)

        df = pd.DataFrame([row[:11] for row in raw], columns=KLINES_COLUMNS)
        for col in _FLOAT_COLUMNS:
            df[col] = df[col].astype(float)
        df["nb_trades"] = df["nb_trades"].astype(int)
        # `unit="ms", utc=True` produit des datetimes tz-aware UTC, comparables
        # sans erreur à `self._now()` (également tz-aware UTC).
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)

        # Exclusion systématique de la bougie en cours (non clôturée) : biais de look-ahead (§2.2).
        now = self._now()
        return df[df["close_time"] < now].reset_index(drop=True)

    def get_klines(self, symbol: str, interval: str) -> pd.DataFrame:
        """Bougies clôturées pour (symbol, interval), via cache incrémental (§2.5).

        `cache.mode: force_refresh` ignore le cache et retélécharge
        `history.limit` bougies. En mode `incremental` (défaut), seul le delta
        depuis la dernière bougie en cache est retéléchargé.
        """
        cache_dir = self._config.cache.directory
        force_refresh = self._config.cache.mode == "force_refresh"
        cached_df = None if force_refresh else cache.load_klines(cache_dir, symbol, interval)

        start_time_ms = None
        if cached_df is not None and not cached_df.empty:
            last_close = cached_df["close_time"].max()
            start_time_ms = int(last_close.timestamp() * 1000) + 1

        fresh_df = self._fetch_klines_raw(
            symbol, interval, limit=self._config.history.limit, start_time_ms=start_time_ms
        )
        merged = cache.merge_tables(cached_df, fresh_df)
        # Si `fresh_df` est vide (aucune nouvelle bougie clôturée depuis le
        # dernier scan — la norme sur 1w/1M), `merged` == cache existant :
        # ne pas réécrire le fichier pour rien.
        if not fresh_df.empty and not merged.empty:
            cache.save_klines(merged, cache_dir, symbol, interval)
        return merged
