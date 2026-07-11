"""Moteur de scoring et consolidation multi-échelles — approche B (§4 CDC).

Entrée : les DataFrames d'indicateurs par TF produits par le Lot 2
(`indicators.IndicatorResult`). Ce module ne fait AUCUN appel réseau, AUCUNE
lecture de cache : il est testable en isolation avec des DataFrames
construits à la main.

Principe directeur (validé) : un indicateur est soit OMIS (donnée
insuffisante → exclu du dénominateur, renormalisation), soit ACTIF MAIS
NEUTRE (donnée suffisante, contribution 0 car aucun signal détecté → compte
normalement dans la moyenne). Jamais de 0 silencieux en lieu et place d'une
donnée manquante.

La même logique de renormalisation s'applique à trois niveaux emboîtés :
règle → catégorie (`_category_scores` + `_effective_category_weights`),
catégorie → score directionnel du TF (`s_t`), TF → tier (`_tier_score`).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

from .config import AppConfig, CategoryWeightsCfg
from .indicators import IndicatorResult

Classe = Literal["haussier", "neutre", "baissier"]


# --------------------------------------------------------------------------- #
# Décomposition (§4.7 CDC) — transparence totale                              #
# --------------------------------------------------------------------------- #
@dataclass
class RuleOutcome:
    """Résultat d'une règle évaluée sur une bougie donnée."""

    rule: str
    label: str
    contribution: float | None  # None si non-scorante (squeeze, doji) ou données insuffisantes
    scoring: bool  # False = diagnostic seul, n'entre jamais dans le score


@dataclass
class TimeframeScore:
    """Score directionnel d'un TF + décomposition complète."""

    s: float | None  # None si le TF est retiré (gates.min_bars_per_tf ou aucune catégorie active)
    category_scores: dict[str, float]
    rule_outcomes: list[RuleOutcome]
    removed: bool
    omitted_indicators: list[str]


@dataclass
class PairScoreResult:
    """Score consolidé d'une paire (0-100) + décomposition complète (§4.7)."""

    score: float
    level: Literal["neutre", "watch", "signal"]
    excluded: bool
    exclusion_reason: str | None
    context_insufficient: bool
    biais_class: Classe | None
    reference_class: Classe | None
    declenchement_score: float | None
    alignment_multiplier: float
    timeframe_scores: dict[str, TimeframeScore]
    flags: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Registre des règles — chaque règle est une fonction pure                    #
# (dernière bougie, bougie précédente ou None, config indicateurs) -> outcome #
# Retourne None si l'indicateur requis est indisponible (donnée insuffisante  #
# ou franchissement/pente impossible à calculer faute de bougie précédente).  #
# --------------------------------------------------------------------------- #
RuleFunc = Callable[[pd.Series, "pd.Series | None", "AppConfig"], "RuleOutcome | None"]


def _missing(row: pd.Series, *columns: str) -> bool:
    """Une colonne est manquante si absente, NaN, OU infinie.

    Une valeur infinie (ex. %B né d'une division par une largeur de bande
    nulle) ne doit jamais franchir la couche indicateurs vers le scoring :
    filet de sécurité en plus du traitement à la source dans indicators.py.
    """
    for c in columns:
        value = row.get(c)
        if pd.isna(value) or np.isinf(value):
            return True
    return False


# --- Tendance ---
def _rule_alignement_ema(last: pd.Series, prev: pd.Series | None, cfg: AppConfig) -> RuleOutcome | None:
    labels = [f"ema_{p}" for p in cfg.indicators.ema]
    if _missing(last, *labels):
        return None
    short, mid, long = (last[label] for label in labels)
    if short > mid > long:
        return RuleOutcome("alignement_ema", "Alignement EMA haussier (courte > moyenne > longue)", 1.0, True)
    if short < mid < long:
        return RuleOutcome("alignement_ema", "Alignement EMA baissier (courte < moyenne < longue)", -1.0, True)
    return RuleOutcome("alignement_ema", "EMA non alignées", 0.0, True)


def _rule_sar_position(last: pd.Series, prev: pd.Series | None, cfg: AppConfig) -> RuleOutcome | None:
    if _missing(last, "sar", "close"):
        return None
    if last["close"] > last["sar"]:
        return RuleOutcome("sar_position", "SAR sous le prix (support)", 1.0, True)
    if last["close"] < last["sar"]:
        return RuleOutcome("sar_position", "SAR au-dessus du prix (résistance)", -1.0, True)
    return RuleOutcome("sar_position", "SAR égal au prix", 0.0, True)


def _rule_prix_vs_ema200(last: pd.Series, prev: pd.Series | None, cfg: AppConfig) -> RuleOutcome | None:
    label = f"ema_{cfg.indicators.ema[-1]}"
    if _missing(last, label, "close"):
        return None
    if last["close"] > last[label]:
        return RuleOutcome("prix_vs_ema200", f"Prix > EMA{cfg.indicators.ema[-1]}", 1.0, True)
    if last["close"] < last[label]:
        return RuleOutcome("prix_vs_ema200", f"Prix < EMA{cfg.indicators.ema[-1]}", -1.0, True)
    return RuleOutcome("prix_vs_ema200", f"Prix == EMA{cfg.indicators.ema[-1]}", 0.0, True)


# --- Momentum ---
def _rule_macd_signal_histogram(last: pd.Series, prev: pd.Series | None, cfg: AppConfig) -> RuleOutcome | None:
    if prev is None or _missing(last, "macd", "macd_signal", "macd_hist") or _missing(prev, "macd_hist"):
        return None
    hist_delta = last["macd_hist"] - prev["macd_hist"]
    if last["macd"] > last["macd_signal"] and hist_delta > 0:
        return RuleOutcome("macd_signal_histogram", "MACD > signal, histogramme croissant", 1.0, True)
    if last["macd"] < last["macd_signal"] and hist_delta < 0:
        return RuleOutcome("macd_signal_histogram", "MACD < signal, histogramme décroissant", -1.0, True)
    return RuleOutcome("macd_signal_histogram", "MACD/histogramme : signal mixte", 0.0, True)


def _rule_rsi_sortie_extreme(last: pd.Series, prev: pd.Series | None, cfg: AppConfig) -> RuleOutcome | None:
    if prev is None or _missing(last, "rsi") or _missing(prev, "rsi"):
        return None
    oversold, overbought = cfg.indicators.rsi.oversold, cfg.indicators.rsi.overbought
    if prev["rsi"] < oversold <= last["rsi"]:
        return RuleOutcome("rsi_sortie_extreme", f"RSI sort de la zone de survente (<{oversold:g})", 1.0, True)
    if prev["rsi"] > overbought >= last["rsi"]:
        return RuleOutcome("rsi_sortie_extreme", f"RSI sort de la zone de surachat (>{overbought:g})", -1.0, True)
    return RuleOutcome("rsi_sortie_extreme", "RSI : pas de sortie de zone extrême", 0.0, True)


def _rule_momentum_signe_pente(last: pd.Series, prev: pd.Series | None, cfg: AppConfig) -> RuleOutcome | None:
    # Signe + pente uniquement (§3.2) : MOM n'est jamais comparable en valeur brute entre paires.
    if prev is None or _missing(last, "mom") or _missing(prev, "mom"):
        return None
    if last["mom"] > 0 and last["mom"] > prev["mom"]:
        return RuleOutcome("momentum_signe_pente", "Momentum positif et croissant", 1.0, True)
    if last["mom"] < 0 and last["mom"] < prev["mom"]:
        return RuleOutcome("momentum_signe_pente", "Momentum négatif et décroissant", -1.0, True)
    return RuleOutcome("momentum_signe_pente", "Momentum mixte/plat", 0.0, True)


# --- Volatilité ---
def _rule_percent_b_reversion(last: pd.Series, prev: pd.Series | None, cfg: AppConfig) -> RuleOutcome | None:
    if _missing(last, "percent_b"):
        return None
    percent_b = last["percent_b"]
    contribution = max(-1.0, min(1.0, 1 - 2 * percent_b))
    if percent_b <= 0.2:
        label = f"%B bas ({percent_b:.2f}) : proche/sous bande inférieure"
    elif percent_b >= 0.8:
        label = f"%B haut ({percent_b:.2f}) : proche/sous bande supérieure"
    else:
        label = f"%B neutre ({percent_b:.2f})"
    return RuleOutcome("percent_b_reversion", label, contribution, True)


def _rule_squeeze(last: pd.Series, prev: pd.Series | None, cfg: AppConfig) -> RuleOutcome | None:
    # Volatilité, pas une direction : diagnostic seul, jamais scorant (cf. plan validé).
    if _missing(last, "bb_width"):
        return None
    return RuleOutcome("squeeze", f"Largeur des bandes bb_width={last['bb_width']:.4f}", None, False)


# --- Volume : confirmation de la variation récente, jamais une direction propre ---
def _rule_volume_confirmation(last: pd.Series, prev: pd.Series | None, cfg: AppConfig) -> RuleOutcome | None:
    if prev is None or _missing(last, "volume", "volume_sma") or _missing(prev, "close"):
        return None
    if last["volume"] <= last["volume_sma"]:
        return RuleOutcome("volume_confirmation", "Volume sous sa MM : pas de confirmation", 0.0, True)
    if last["close"] > prev["close"]:
        return RuleOutcome("volume_confirmation", "Volume au-dessus de sa MM, confirme la hausse", 1.0, True)
    if last["close"] < prev["close"]:
        return RuleOutcome("volume_confirmation", "Volume au-dessus de sa MM, confirme la baisse", -1.0, True)
    return RuleOutcome("volume_confirmation", "Volume au-dessus de sa MM, prix stable", 0.0, True)


# --- Patterns : CDL* haussier/baissier — le doji n'est jamais directionnel (vérifié empiriquement) ---
def _make_cdl_rule(column: str, scoring: bool) -> RuleFunc:
    def rule(last: pd.Series, prev: pd.Series | None, cfg: AppConfig) -> RuleOutcome | None:
        if _missing(last, column):
            return None
        value = last[column]
        if not scoring:
            detected = "détecté" if value != 0 else "absent"
            return RuleOutcome(column, f"{column} : {detected} (non directionnel)", None, False)
        if value > 0:
            return RuleOutcome(column, f"{column} haussier détecté", 1.0, True)
        if value < 0:
            return RuleOutcome(column, f"{column} baissier détecté", -1.0, True)
        return RuleOutcome(column, f"{column} : pas de figure", 0.0, True)

    return rule


RULES: tuple[tuple[str, RuleFunc], ...] = (
    ("trend", _rule_alignement_ema),
    ("trend", _rule_sar_position),
    ("trend", _rule_prix_vs_ema200),
    ("momentum", _rule_macd_signal_histogram),
    ("momentum", _rule_rsi_sortie_extreme),
    ("momentum", _rule_momentum_signe_pente),
    ("volatility", _rule_percent_b_reversion),
    ("volatility", _rule_squeeze),
    ("volume", _rule_volume_confirmation),
    ("patterns", _make_cdl_rule("cdl_engulfing", scoring=True)),
    ("patterns", _make_cdl_rule("cdl_hammer", scoring=True)),
    ("patterns", _make_cdl_rule("cdl_morning_star", scoring=True)),
    ("patterns", _make_cdl_rule("cdl_shooting_star", scoring=True)),
    ("patterns", _make_cdl_rule("cdl_doji", scoring=False)),
)


def _evaluate_rules(last: pd.Series, prev: pd.Series | None, cfg: AppConfig) -> list[tuple[str, RuleOutcome]]:
    outcomes = []
    for category, rule_fn in RULES:
        outcome = rule_fn(last, prev, cfg)
        if outcome is not None:
            outcomes.append((category, outcome))
    return outcomes


def _category_scores(outcomes: list[tuple[str, RuleOutcome]]) -> dict[str, float]:
    """Moyenne uniforme des contributions SCORANTES actives, par catégorie.

    Une catégorie absente du résultat = aucune règle scorante active
    (omission complète) : elle sera exclue de `_effective_category_weights`.
    """
    by_category: dict[str, list[float]] = {}
    for category, outcome in outcomes:
        if outcome.scoring and outcome.contribution is not None:
            by_category.setdefault(category, []).append(outcome.contribution)
    return {cat: sum(vals) / len(vals) for cat, vals in by_category.items() if vals}


def _effective_category_weights(
    category_scores: dict[str, float], adx_factor: float, cfg: CategoryWeightsCfg
) -> dict[str, float]:
    """Poids effectifs des catégories actives, ADX-modulé pour `trend`, renormalisés (somme=1).

    Reconstruit intégralement le dict à chaque appel à partir des seules
    catégories actives : aucun reliquat possible, aucune division par zéro
    (le cas `total<=0`, y compris "toutes catégories omises", retourne `{}`).
    """
    nominal = {
        "trend": cfg.trend * adx_factor,
        "momentum": cfg.momentum,
        "volatility": cfg.volatility,
        "volume": cfg.volume,
        "patterns": cfg.patterns,
    }
    active = {cat: w for cat, w in nominal.items() if cat in category_scores}
    total = sum(active.values())
    if total <= 0:
        return {}
    return {cat: w / total for cat, w in active.items()}


# --------------------------------------------------------------------------- #
# Score directionnel par TF                                                   #
# --------------------------------------------------------------------------- #
def score_timeframe(indicator_result: IndicatorResult, config: AppConfig) -> TimeframeScore:
    """Score directionnel `s_t` d'un TF (§4.2 CDC), à partir des indicateurs du Lot 2."""
    df = indicator_result.data

    if len(df) < config.gates.min_bars_per_tf:
        return TimeframeScore(
            s=None, category_scores={}, rule_outcomes=[], removed=True,
            omitted_indicators=indicator_result.omitted,
        )

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else None

    raw_outcomes = _evaluate_rules(last, prev, config)
    category_scores = _category_scores(raw_outcomes)

    adx_factor = 1.0  # ADX omis => pas de modulation, poids de config tels quels
    if not _missing(last, "adx"):
        adx_factor = last["adx"] / config.indicators.adx.trend_threshold

    weights = _effective_category_weights(category_scores, adx_factor, config.category_weights)
    rule_outcomes = [outcome for _category, outcome in raw_outcomes]

    if not weights:
        return TimeframeScore(
            s=None, category_scores=category_scores, rule_outcomes=rule_outcomes, removed=True,
            omitted_indicators=indicator_result.omitted,
        )

    s = sum(weights[cat] * category_scores[cat] for cat in weights)
    return TimeframeScore(
        s=s, category_scores=category_scores, rule_outcomes=rule_outcomes, removed=False,
        omitted_indicators=indicator_result.omitted,
    )


# --------------------------------------------------------------------------- #
# Consolidation multi-échelles (§4.3/4.4) : tiers, classification, m, score   #
# --------------------------------------------------------------------------- #
def _classify(score: float, neutral_band: float) -> Classe:
    if score >= neutral_band:
        return "haussier"
    if score <= -neutral_band:
        return "baissier"
    return "neutre"


def _tier_score(tf_scores: dict[str, TimeframeScore], weights: dict[str, float]) -> float | None:
    """Moyenne pondérée des `s_t` disponibles du tier, renormalisée si un TF manque.

    Retourne None si AUCUN TF du tier n'est disponible (poids résiduel nul).
    """
    active = {
        tf: w for tf, w in weights.items()
        if tf_scores.get(tf) is not None and tf_scores[tf].s is not None
    }
    total = sum(active.values())
    if total <= 0:
        return None
    return sum((active[tf] / total) * tf_scores[tf].s for tf in active)


def _alignment_multiplier(biais_class: Classe, reference_class: Classe, config: AppConfig) -> float:
    """Précédence stricte : un tier baissier (biais OU référence) impose la contradiction,
    même si l'autre tier est haussier (confirmé — voir tableau de vérité dans le plan)."""
    cfg = config.alignment_multiplier
    if biais_class == "baissier" or reference_class == "baissier":
        return cfg.contradiction
    if biais_class == "haussier" and reference_class == "haussier":
        return cfg.full_align
    if biais_class == "haussier" or reference_class == "haussier":
        return cfg.partial
    return cfg.neutral


def score_pair(timeframe_indicators: dict[str, IndicatorResult], config: AppConfig) -> PairScoreResult:
    """Score consolidé d'une paire (§4.3/4.4 CDC), à partir des indicateurs des 5 TF.

    `timeframe_indicators` : clés attendues = valeurs de `config.intervals`
    ("4h", "12h", "1d", "1w", "1M"), chacune un `IndicatorResult` du Lot 2.
    """
    tf_scores = {
        tf: score_timeframe(result, config) for tf, result in timeframe_indicators.items()
    }

    declenchement_weights = config.tiers.declenchement.timeframes
    D = _tier_score(tf_scores, declenchement_weights)
    if D is None:
        # Gate dur (§4.5) : aucune donnée exploitable sur le tier déclenchement => paire exclue.
        return PairScoreResult(
            score=0.0, level="neutre", excluded=True,
            exclusion_reason="aucune donnée exploitable sur le tier déclenchement",
            context_insufficient=False, biais_class=None, reference_class=None,
            declenchement_score=None, alignment_multiplier=0.0,
            timeframe_scores=tf_scores, flags=["exclue : déclenchement indisponible"],
        )

    biais_fond_weights = config.tiers.biais_fond.timeframes
    biais_score = _tier_score(tf_scores, biais_fond_weights)
    context_insufficient = biais_score is None
    neutral_band = config.classification.neutral_band
    biais_class = _classify(biais_score, neutral_band) if biais_score is not None else None

    reference_tf = config.tiers.reference.timeframe
    reference_tf_score = tf_scores.get(reference_tf)
    reference_score = reference_tf_score.s if reference_tf_score is not None else None
    reference_class = _classify(reference_score, neutral_band) if reference_score is not None else None

    # Biais/référence indéterminés => traités comme neutres pour le multiplicateur m
    # (décision validée) ; le contexte insuffisant est pénalisé séparément ci-dessous.
    m = _alignment_multiplier(biais_class or "neutre", reference_class or "neutre", config)

    score_brut = max(D, 0.0) * m  # porte long-only : D<=0 => score_brut=0 quel que soit m
    facteur_contexte = config.context_insufficient_factor if context_insufficient else 1.0
    score_final = 100.0 * score_brut * facteur_contexte

    flags = ["contexte insuffisant"] if context_insufficient else []

    level: Literal["neutre", "watch", "signal"] = "neutre"
    if score_final >= config.thresholds.signal:
        level = "signal"
    elif score_final >= config.thresholds.watch:
        level = "watch"

    return PairScoreResult(
        score=score_final, level=level, excluded=False, exclusion_reason=None,
        context_insufficient=context_insufficient, biais_class=biais_class, reference_class=reference_class,
        declenchement_score=D, alignment_multiplier=m, timeframe_scores=tf_scores, flags=flags,
    )
