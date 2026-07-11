"""Orchestrateur du scan (§1.2, §6 CDC).

Univers */USDC -> gate de liquidité (D6) -> par paire retenue : bougies des
5 TF (cache incrémental du Lot 1) -> indicateurs (Lot 2) -> score (Lot 3).

Ne réimplémente rien : ce module appelle uniquement `data_fetcher`,
`indicators` et `scoring_engine`. Séquentiel (§2.4 CDC : "séquentiel +
gouverneur d'abord ; parallélisation bornée = optimisation ultérieure").
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import AppConfig
from .data_fetcher import DataFetcher
from .indicators import compute_indicators
from .rate_limiter import BinanceBannedError, RateLimiter
from .scoring_engine import PairScoreResult, score_pair

logger = logging.getLogger(__name__)


@dataclass
class ScanRow:
    """Une paire retenue et effectivement scorée (ni exclue, ni en erreur)."""

    symbol: str
    result: PairScoreResult
    close: float | None
    quote_volume_24h: float
    atr_pct: float | None
    rsi_1d: float | None
    adx_1d: float | None


@dataclass
class ScanSummary:
    """Statistiques et journalisation du scan (§1.3 CDC : traçabilité)."""

    universe_size: int
    qualifying_count: int  # après gate D6
    scored_count: int  # paires effectivement présentes dans `rows`
    excluded_count: int  # exclues par score_pair (déclenchement inexploitable)
    failed_symbols: dict[str, str] = field(default_factory=dict)  # symbole -> message d'erreur
    total_weight_consumed: int = 0  # delta de CE scan uniquement, jamais cumulatif inter-scans
    scan_timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ScanResult:
    rows: list[ScanRow]  # triées par score décroissant
    summary: ScanSummary


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def run_scan(
    config: AppConfig,
    fetcher: DataFetcher | None = None,
    rate_limiter: RateLimiter | None = None,
    now_func: Callable[[], datetime] = _utc_now,
) -> ScanResult:
    """Lance un scan complet et retourne un `ScanResult` prêt pour `reporting`.

    `fetcher`/`rate_limiter` injectables (tests, mocks) ; construits depuis
    `config` si omis. `total_weight_consumed` est calculé en delta (valeur de
    fin − valeur de début du `RateLimiter`), pas en lecture brute : un
    `RateLimiter` réutilisé pour plusieurs scans dans le même processus ne
    doit jamais faire remonter un poids gonflé par les scans précédents.
    """
    owns_fetcher = fetcher is None
    if rate_limiter is None:
        rate_limiter = RateLimiter(
            budget_per_minute=config.rate_limiter.budget_per_minute,
            max_retries=config.rate_limiter.max_retries,
            backoff_base_seconds=config.rate_limiter.backoff_base_seconds,
        )
    if fetcher is None:
        fetcher = DataFetcher(config=config, rate_limiter=rate_limiter)

    weight_at_start = rate_limiter.total_consumed
    reference_tf = config.tiers.reference.timeframe

    try:
        universe = fetcher.fetch_usdc_universe()
        volumes = fetcher.fetch_24h_quote_volumes()

        min_volume = config.gates.min_quote_volume_24h
        qualifying = [s for s in universe if volumes.get(s, 0.0) >= min_volume]
        logger.info(
            "Gate D6 (liquidité) : %d/%d paires */USDC retenues (>= %.0f USDC/24h)",
            len(qualifying), len(universe), min_volume,
        )

        rows: list[ScanRow] = []
        excluded_count = 0
        failed_symbols: dict[str, str] = {}

        for symbol in qualifying:
            try:
                timeframe_indicators = {
                    interval: compute_indicators(
                        fetcher.get_klines(symbol, interval), config.indicators, config.gates
                    )
                    for interval in config.intervals
                }
                result = score_pair(timeframe_indicators, config)

                if result.excluded:
                    excluded_count += 1
                    logger.info("Paire %s exclue : %s", symbol, result.exclusion_reason)
                    continue

                reference_data = timeframe_indicators.get(reference_tf)
                close = atr_pct = rsi_1d = adx_1d = None
                if reference_data is not None and not reference_data.data.empty:
                    last = reference_data.data.iloc[-1]
                    close = last.get("close")
                    atr_pct = last.get("atr_pct") if "atr_pct" in reference_data.data.columns else None
                    rsi_1d = last.get("rsi") if "rsi" in reference_data.data.columns else None
                    adx_1d = last.get("adx") if "adx" in reference_data.data.columns else None

                rows.append(
                    ScanRow(
                        symbol=symbol, result=result, close=close,
                        quote_volume_24h=volumes.get(symbol, 0.0),
                        atr_pct=atr_pct, rsi_1d=rsi_1d, adx_1d=adx_1d,
                    )
                )
            except BinanceBannedError:
                # Un ban affecte tout le scan, pas une seule paire : on n'insiste pas
                # sur les paires suivantes (§2.4 CDC — éviter la boucle de retry).
                logger.critical("Scan interrompu sur %s : IP bannie par Binance (418).", symbol)
                raise
            except Exception as exc:  # noqa: BLE001 - robustesse : une paire ne tue jamais le scan
                logger.error("Paire %s ignorée après erreur : %r", symbol, exc)
                failed_symbols[symbol] = repr(exc)
                continue

        rows.sort(key=lambda row: row.result.score, reverse=True)

        total_weight_consumed = rate_limiter.total_consumed - weight_at_start
        logger.info(
            "Scan terminé : %d paires scorées, %d exclues, %d en erreur, poids consommé = %d",
            len(rows), excluded_count, len(failed_symbols), total_weight_consumed,
        )

        summary = ScanSummary(
            universe_size=len(universe),
            qualifying_count=len(qualifying),
            scored_count=len(rows),
            excluded_count=excluded_count,
            failed_symbols=failed_symbols,
            total_weight_consumed=total_weight_consumed,
            scan_timestamp=now_func(),
        )
        return ScanResult(rows=rows, summary=summary)
    finally:
        if owns_fetcher:
            fetcher.close()
