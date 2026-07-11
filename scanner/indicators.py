"""Indicateurs techniques — couche mince au-dessus de TA-Lib (§3 CDC).

Aucun indicateur n'est réimplémenté à la main : chaque calcul délègue à
TA-Lib. Toutes les périodes viennent de `config.yaml` (`indicators_cfg`),
jamais en dur.

Dégradation gracieuse (§3.3) : un indicateur dont la période dépasse
l'historique disponible est OMIS (colonne absente), jamais rempli à 0. TA-Lib
retourne nativement `NaN` sur toute la sortie quand l'historique est
insuffisant (vérifié empiriquement, sans exception) : on s'appuie sur ce
comportement plutôt que de recalculer nous-mêmes chaque formule de lookback.
Seule l'EMA "longue" (200 par défaut) a une garde dédiée explicite
(`gates.ema200_min_bars`), car c'est un seuil de config nommé, découplé de la
période elle-même.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import talib

from .config import GatesCfg, IndicatorsCfg

# (fonction TA-Lib, nom de colonne) — figures chandeliers verrouillées (§3.2).
CANDLESTICK_PATTERNS: tuple[tuple[str, str], ...] = (
    ("CDLENGULFING", "cdl_engulfing"),
    ("CDLHAMMER", "cdl_hammer"),
    ("CDLMORNINGSTAR", "cdl_morning_star"),
    ("CDLSHOOTINGSTAR", "cdl_shooting_star"),
    ("CDLDOJI", "cdl_doji"),
)


@dataclass
class IndicatorResult:
    """Sortie de `compute_indicators` : données enrichies + indicateurs omis (§3.3 CDC)."""

    data: pd.DataFrame
    omitted: list[str] = field(default_factory=list)


def _to_f64(series: pd.Series) -> np.ndarray:
    """Tableau numpy float64 contigu — requis par TA-Lib."""
    return np.ascontiguousarray(series.to_numpy(dtype=np.float64))


def _last_is_valid(values: np.ndarray) -> bool:
    """Le dernier point est-il exploitable (historique suffisant, pas de NaN de chauffe) ?"""
    return len(values) > 0 and not np.isnan(values[-1])


def compute_indicators(
    df: pd.DataFrame, indicators_cfg: IndicatorsCfg, gates_cfg: GatesCfg
) -> IndicatorResult:
    """Enrichit un DataFrame OHLCV d'un TF avec le jeu d'indicateurs verrouillé (§3.2 CDC).

    L'entrée est retriée par `open_time` croissant avant tout calcul. Les
    NaN de chauffe en début de colonne (par ailleurs conservée) ne sont pas
    supprimés : leur exclusion revient au moteur de scoring (Lot 3), pas à
    cette couche.
    """
    data = df.sort_values("open_time").reset_index(drop=True).copy()
    omitted: list[str] = []

    open_ = _to_f64(data["open"])
    high = _to_f64(data["high"])
    low = _to_f64(data["low"])
    close = _to_f64(data["close"])
    volume = _to_f64(data["volume"])

    # --- Moyennes mobiles EMA courte/moyenne/longue (D4) ---
    ema_periods = indicators_cfg.ema
    ema_labels = [f"ema_{p}" for p in ema_periods]
    long_period = ema_periods[-1]
    for period, label in zip(ema_periods, ema_labels):
        if period == long_period and len(data) < gates_cfg.ema200_min_bars:
            # Garde dédiée §3.3 : seuil de config nommé, découplé de la période.
            omitted.append(label)
            continue
        values = talib.EMA(close, timeperiod=period)
        if _last_is_valid(values):
            data[label] = values
        else:
            omitted.append(label)

    # --- RSI ---
    rsi_values = talib.RSI(close, timeperiod=indicators_cfg.rsi.period)
    if _last_is_valid(rsi_values):
        data["rsi"] = rsi_values
    else:
        omitted.append("rsi")

    # --- MACD ---
    macd_line, macd_signal, macd_hist = talib.MACD(
        close,
        fastperiod=indicators_cfg.macd.fast,
        slowperiod=indicators_cfg.macd.slow,
        signalperiod=indicators_cfg.macd.signal,
    )
    if _last_is_valid(macd_hist):
        data["macd"] = macd_line
        data["macd_signal"] = macd_signal
        data["macd_hist"] = macd_hist
    else:
        omitted.append("macd")

    # --- Parabolic SAR ---
    sar_values = talib.SAR(high, low, acceleration=indicators_cfg.sar.step, maximum=indicators_cfg.sar.max)
    if _last_is_valid(sar_values):
        data["sar"] = sar_values
    else:
        omitted.append("sar")

    # --- Bandes de Bollinger + dérivés (largeur, %B) ---
    bb_upper, bb_middle, bb_lower = talib.BBANDS(
        close,
        timeperiod=indicators_cfg.bbands.period,
        nbdevup=indicators_cfg.bbands.stddev,
        nbdevdn=indicators_cfg.bbands.stddev,
        matype=0,
    )
    band_range = bb_upper - bb_lower
    # Largeur de bande nulle sur la dernière bougie (précision flottante à prix
    # ultra-faible, ex. memecoins à quelques millionièmes d'USDC : bb_upper==bb_lower
    # exactement) : %B = (close-inf)/0 serait infini. Traité comme "bbands omis" pour
    # cette bougie plutôt que de fabriquer un signal à partir d'une donnée indéfinie.
    if _last_is_valid(bb_upper) and band_range[-1] != 0:
        data["bb_upper"] = bb_upper
        data["bb_middle"] = bb_middle
        data["bb_lower"] = bb_lower
        data["bb_width"] = band_range / bb_middle  # largeur = (sup-inf)/moyenne
        data["percent_b"] = (close - bb_lower) / band_range  # %B = (close-inf)/(sup-inf)
    else:
        omitted.append("bbands")

    # --- Momentum (MOM) ---
    # Sortie = différence de prix NON BORNÉE (unités de prix), donc non
    # comparable d'une paire à l'autre (§3.2 CDC). On garde ici la valeur
    # brute (utile à l'affichage/diagnostic) ; le Lot 3 (scoring) ne devra
    # JAMAIS l'utiliser en valeur absolue — seulement son signe et sa pente
    # (positif et croissant = momentum haussier).
    mom_values = talib.MOM(close, timeperiod=indicators_cfg.momentum.period)
    if _last_is_valid(mom_values):
        data["mom"] = mom_values
    else:
        omitted.append("mom")

    # --- Volume vs moyenne mobile de volume ---
    volume_sma = talib.SMA(volume, timeperiod=indicators_cfg.volume_ma.period)
    if _last_is_valid(volume_sma):
        data["volume_sma"] = volume_sma
    else:
        omitted.append("volume_sma")

    # --- ADX (+DI/-DI) : filtre de régime / force de tendance ---
    adx_values = talib.ADX(high, low, close, timeperiod=indicators_cfg.adx.period)
    if _last_is_valid(adx_values):
        data["adx"] = adx_values
        data["plus_di"] = talib.PLUS_DI(high, low, close, timeperiod=indicators_cfg.adx.period)
        data["minus_di"] = talib.MINUS_DI(high, low, close, timeperiod=indicators_cfg.adx.period)
    else:
        omitted.append("adx")

    # --- ATR : risque/sortie uniquement, HORS SCORE (§3.2) ---
    atr_values = talib.ATR(high, low, close, timeperiod=indicators_cfg.atr.period)
    if _last_is_valid(atr_values):
        data["atr"] = atr_values
        data["atr_pct"] = atr_values / close  # ATR% = ATR/close
    else:
        omitted.append("atr")

    # --- Figures chandeliers (CDL*) ---
    # Toujours calculées, sans logique d'omission : TA-Lib encode nativement
    # "pas de figure / historique insuffisant" par 0, qui est déjà la
    # sémantique correcte de la librairie (pas une valeur neutre inventée).
    for talib_name, column_label in CANDLESTICK_PATTERNS:
        fn = getattr(talib, talib_name)
        data[column_label] = fn(open_, high, low, close)

    # --- Dérivé : alignement EMA courte > moyenne > longue ---
    # Omis si l'une des 3 EMA est absente (ne peut pas être calculé partiellement).
    if all(label in data.columns for label in ema_labels):
        data["ema_aligned_bullish"] = (data[ema_labels[0]] > data[ema_labels[1]]) & (
            data[ema_labels[1]] > data[ema_labels[2]]
        )
    else:
        omitted.append("ema_aligned_bullish")

    return IndicatorResult(data=data, omitted=omitted)
