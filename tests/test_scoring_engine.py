"""Tests du moteur de scoring et de la consolidation multi-échelles (§4 CDC, Lot 3)."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from scanner.config import load_config
from scanner.indicators import IndicatorResult, compute_indicators
from scanner.scoring_engine import (
    PairScoreResult,
    RuleOutcome,
    TimeframeScore,
    _alignment_multiplier,
    _category_scores,
    _classify,
    _effective_category_weights,
    _make_cdl_rule,
    _missing,
    _rule_alignement_ema,
    _rule_macd_signal_histogram,
    _rule_momentum_signe_pente,
    _rule_percent_b_reversion,
    _rule_prix_vs_ema200,
    _rule_rsi_sortie_extreme,
    _rule_sar_position,
    _rule_squeeze,
    _rule_volume_confirmation,
    _tier_score,
    score_pair,
    score_timeframe,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
_CFG = load_config(CONFIG_PATH)  # utilisé par les builders de lignes (valeurs de config uniquement)


@pytest.fixture
def cfg():
    return load_config(CONFIG_PATH)


@pytest.fixture
def cfg_relaxed(cfg):
    """gates.min_bars_per_tf abaissé : permet des DataFrames de test à 2 lignes sans être
    retirés par le gate grossier, pour isoler le comportement au niveau des règles/catégories."""
    return cfg.model_copy(update={"gates": cfg.gates.model_copy(update={"min_bars_per_tf": 2})})


def _row(**overrides) -> pd.Series:
    base = {
        "close": 100.0, "sar": None,
        "ema_20": None, "ema_50": None, "ema_200": None,
        "rsi": None, "macd": None, "macd_signal": None, "macd_hist": None,
        "mom": None, "percent_b": None, "bb_width": None,
        "volume": None, "volume_sma": None, "adx": None,
        "cdl_engulfing": None, "cdl_hammer": None, "cdl_morning_star": None,
        "cdl_shooting_star": None, "cdl_doji": None,
    }
    base.update(overrides)
    return pd.Series(base)


# =========================================================================== #
# Garde-fou _missing() : NaN ET infinis (filet de sécurité, cf. bug %B)       #
# =========================================================================== #
def test_missing_helper_treats_nan_as_missing():
    assert _missing(_row(rsi=float("nan")), "rsi") is True


def test_missing_helper_treats_positive_infinity_as_missing():
    assert _missing(_row(percent_b=float("inf")), "percent_b") is True


def test_missing_helper_treats_negative_infinity_as_missing():
    assert _missing(_row(rsi=float("-inf")), "rsi") is True


def test_missing_helper_false_for_finite_value():
    assert _missing(_row(rsi=50.0), "rsi") is False


def test_percent_b_rule_omitted_when_infinite():
    """Une valeur infinie ne doit jamais franchir la couche indicateurs vers le scoring."""
    assert _rule_percent_b_reversion(_row(percent_b=float("inf")), None, _CFG) is None


# =========================================================================== #
# Règles — Tendance                                                           #
# =========================================================================== #
def test_alignement_ema_bullish():
    row = _row(ema_20=110, ema_50=105, ema_200=100)
    assert _rule_alignement_ema(row, None, _CFG).contribution == 1.0


def test_alignement_ema_bearish():
    row = _row(ema_20=95, ema_50=100, ema_200=105)
    assert _rule_alignement_ema(row, None, _CFG).contribution == -1.0


def test_alignement_ema_mixed_is_neutral():
    row = _row(ema_20=105, ema_50=95, ema_200=100)
    assert _rule_alignement_ema(row, None, _CFG).contribution == 0.0


def test_alignement_ema_omitted_when_one_ema_missing():
    row = _row(ema_20=110, ema_50=105)  # ema_200 absent
    assert _rule_alignement_ema(row, None, _CFG) is None


def test_sar_position_bullish():
    assert _rule_sar_position(_row(close=100, sar=95), None, _CFG).contribution == 1.0


def test_sar_position_bearish():
    assert _rule_sar_position(_row(close=100, sar=105), None, _CFG).contribution == -1.0


def test_sar_position_omitted_when_missing():
    assert _rule_sar_position(_row(close=100), None, _CFG) is None


def test_prix_vs_ema200_bullish():
    long_period = _CFG.indicators.ema[-1]
    row = _row(close=100, **{f"ema_{long_period}": 90})
    assert _rule_prix_vs_ema200(row, None, _CFG).contribution == 1.0


def test_prix_vs_ema200_bearish():
    long_period = _CFG.indicators.ema[-1]
    row = _row(close=100, **{f"ema_{long_period}": 110})
    assert _rule_prix_vs_ema200(row, None, _CFG).contribution == -1.0


def test_prix_vs_ema200_omitted_when_missing():
    assert _rule_prix_vs_ema200(_row(close=100), None, _CFG) is None


# =========================================================================== #
# Règles — Momentum                                                           #
# =========================================================================== #
def test_macd_bullish_rising_histogram():
    last = _row(macd=1.0, macd_signal=0.5, macd_hist=0.5)
    prev = _row(macd_hist=0.3)
    assert _rule_macd_signal_histogram(last, prev, _CFG).contribution == 1.0


def test_macd_bearish_falling_histogram():
    last = _row(macd=-1.0, macd_signal=-0.5, macd_hist=-0.5)
    prev = _row(macd_hist=-0.3)
    assert _rule_macd_signal_histogram(last, prev, _CFG).contribution == -1.0


def test_macd_mixed_signal_is_neutral():
    last = _row(macd=1.0, macd_signal=0.5, macd_hist=0.5)
    prev = _row(macd_hist=0.6)  # macd > signal mais histogramme décroissant
    assert _rule_macd_signal_histogram(last, prev, _CFG).contribution == 0.0


def test_macd_omitted_without_previous_row():
    last = _row(macd=1.0, macd_signal=0.5, macd_hist=0.5)
    assert _rule_macd_signal_histogram(last, None, _CFG) is None


def test_macd_omitted_at_warmup_boundary_when_prev_hist_is_nan():
    """t valide, t-1 encore en chauffe : la pente est incalculable => règle omise, pas notée 0."""
    last = _row(macd=1.0, macd_signal=0.5, macd_hist=0.5)
    prev = _row()  # macd_hist manquant
    assert _rule_macd_signal_histogram(last, prev, _CFG) is None


def test_rsi_sortie_de_survente_is_bullish():
    oversold = _CFG.indicators.rsi.oversold
    last = _row(rsi=oversold + 1)
    prev = _row(rsi=oversold - 1)
    assert _rule_rsi_sortie_extreme(last, prev, _CFG).contribution == 1.0


def test_rsi_sortie_de_surachat_is_bearish():
    overbought = _CFG.indicators.rsi.overbought
    last = _row(rsi=overbought - 1)
    prev = _row(rsi=overbought + 1)
    assert _rule_rsi_sortie_extreme(last, prev, _CFG).contribution == -1.0


def test_rsi_no_crossing_is_neutral():
    last = _row(rsi=50)
    prev = _row(rsi=48)
    assert _rule_rsi_sortie_extreme(last, prev, _CFG).contribution == 0.0


def test_rsi_high_static_value_is_not_bullish_by_itself():
    """Confirme la résolution retenue (mean-reversion sur sortie de zone), pas une lecture
    'RSI haut = fort' : un RSI élevé mais stable ne déclenche rien."""
    last = _row(rsi=75)
    prev = _row(rsi=74)
    assert _rule_rsi_sortie_extreme(last, prev, _CFG).contribution == 0.0


def test_rsi_omitted_at_warmup_boundary():
    last = _row(rsi=50)
    assert _rule_rsi_sortie_extreme(last, _row(), _CFG) is None
    assert _rule_rsi_sortie_extreme(last, None, _CFG) is None


def test_momentum_positive_and_increasing_is_bullish():
    last = _row(mom=2.0)
    prev = _row(mom=1.0)
    assert _rule_momentum_signe_pente(last, prev, _CFG).contribution == 1.0


def test_momentum_negative_and_decreasing_is_bearish():
    last = _row(mom=-2.0)
    prev = _row(mom=-1.0)
    assert _rule_momentum_signe_pente(last, prev, _CFG).contribution == -1.0


def test_momentum_positive_but_decreasing_is_neutral():
    last = _row(mom=1.0)
    prev = _row(mom=2.0)
    assert _rule_momentum_signe_pente(last, prev, _CFG).contribution == 0.0


# =========================================================================== #
# Règles — Volatilité                                                         #
# =========================================================================== #
def test_percent_b_low_is_bullish():
    assert _rule_percent_b_reversion(_row(percent_b=0.0), None, _CFG).contribution == 1.0


def test_percent_b_high_is_bearish():
    assert _rule_percent_b_reversion(_row(percent_b=1.0), None, _CFG).contribution == -1.0


def test_percent_b_middle_is_neutral():
    assert _rule_percent_b_reversion(_row(percent_b=0.5), None, _CFG).contribution == 0.0


def test_percent_b_clipped_beyond_bands():
    assert _rule_percent_b_reversion(_row(percent_b=-0.5), None, _CFG).contribution == 1.0
    assert _rule_percent_b_reversion(_row(percent_b=1.5), None, _CFG).contribution == -1.0


def test_squeeze_is_never_scoring():
    outcome = _rule_squeeze(_row(bb_width=0.01), None, _CFG)
    assert outcome.scoring is False
    assert outcome.contribution is None


def test_squeeze_omitted_when_missing():
    assert _rule_squeeze(_row(), None, _CFG) is None


# =========================================================================== #
# Règles — Volume (confirmation, jamais une direction propre)                 #
# =========================================================================== #
def test_volume_above_average_confirms_rise():
    last = _row(volume=100, volume_sma=50, close=105)
    prev = _row(close=100)
    assert _rule_volume_confirmation(last, prev, _CFG).contribution == 1.0


def test_volume_above_average_confirms_fall():
    last = _row(volume=100, volume_sma=50, close=95)
    prev = _row(close=100)
    assert _rule_volume_confirmation(last, prev, _CFG).contribution == -1.0


def test_volume_below_average_never_confirms_even_if_price_moves():
    last = _row(volume=10, volume_sma=50, close=105)
    prev = _row(close=100)
    assert _rule_volume_confirmation(last, prev, _CFG).contribution == 0.0


# =========================================================================== #
# Règles — Patterns (CDL*), doji jamais directionnel                          #
# =========================================================================== #
def test_cdl_bullish_pattern_detected():
    rule = _make_cdl_rule("cdl_engulfing", scoring=True)
    assert rule(_row(cdl_engulfing=100), None, _CFG).contribution == 1.0


def test_cdl_bearish_pattern_detected():
    rule = _make_cdl_rule("cdl_engulfing", scoring=True)
    assert rule(_row(cdl_engulfing=-100), None, _CFG).contribution == -1.0


def test_cdl_no_pattern_is_neutral():
    rule = _make_cdl_rule("cdl_engulfing", scoring=True)
    assert rule(_row(cdl_engulfing=0), None, _CFG).contribution == 0.0


def test_cdl_doji_never_scores_even_when_detected():
    rule = _make_cdl_rule("cdl_doji", scoring=False)
    outcome = rule(_row(cdl_doji=100), None, _CFG)
    assert outcome.scoring is False
    assert outcome.contribution is None


# =========================================================================== #
# Agrégation règle -> catégorie, et modulation ADX -> poids effectifs         #
# =========================================================================== #
def test_category_scores_averages_only_scoring_outcomes():
    outcomes = [
        ("trend", RuleOutcome("a", "a", 1.0, True)),
        ("trend", RuleOutcome("b", "b", -1.0, True)),
        ("volatility", RuleOutcome("squeeze", "squeeze", None, False)),
    ]
    result = _category_scores(outcomes)
    assert result == {"trend": 0.0}  # volatility absente : aucune règle scorante active


def test_effective_category_weights_trend_omitted_renormalizes_cleanly(cfg):
    """(b) confirmée : trend entièrement omise => renormalisation propre sur les 4 autres
    catégories, sans division par zéro ni poids résiduel fantôme."""
    category_scores = {"momentum": 0.5, "volatility": -0.2, "volume": 0.3, "patterns": 0.0}
    weights = _effective_category_weights(category_scores, adx_factor=2.0, cfg=cfg.category_weights)

    assert "trend" not in weights
    assert set(weights.keys()) == set(category_scores.keys())
    assert sum(weights.values()) == pytest.approx(1.0)

    nominal = cfg.category_weights
    remaining_total = nominal.momentum + nominal.volatility + nominal.volume + nominal.patterns
    for cat in category_scores:
        expected_share = getattr(nominal, cat) / remaining_total
        assert weights[cat] == pytest.approx(expected_share)


def test_effective_category_weights_no_division_by_zero_when_all_omitted(cfg):
    assert _effective_category_weights({}, adx_factor=1.0, cfg=cfg.category_weights) == {}
    assert _effective_category_weights({}, adx_factor=4.0, cfg=cfg.category_weights) == {}


def test_adx_factor_unbounded_matches_documented_extreme_case(cfg):
    """(a) confirmée : comportement intentionnel, non plafonné. ADX=100, seuil=25 => facteur=4."""
    category_scores = {"trend": 1.0, "momentum": 1.0, "volatility": 1.0, "volume": 1.0, "patterns": 1.0}
    adx_factor = 100.0 / cfg.indicators.adx.trend_threshold
    assert adx_factor == pytest.approx(4.0)

    weights = _effective_category_weights(category_scores, adx_factor=adx_factor, cfg=cfg.category_weights)
    nominal = cfg.category_weights
    nominal_total = nominal.trend * 4.0 + nominal.momentum + nominal.volatility + nominal.volume + nominal.patterns
    expected_trend_share = (nominal.trend * 4.0) / nominal_total

    assert weights["trend"] == pytest.approx(expected_trend_share)
    assert weights["trend"] > nominal.trend  # part de trend augmentée vs. poids nominal
    assert sum(weights.values()) == pytest.approx(1.0)


def test_adx_omitted_falls_back_to_unmodulated_weight(cfg_relaxed):
    """ADX omis (paire très jeune) => facteur neutre 1.0, poids de config inchangés."""
    ema_periods = cfg_relaxed.indicators.ema
    ema_values = {f"ema_{p}": 100.0 + (len(ema_periods) - 1 - i) * 3 for i, p in enumerate(ema_periods)}
    last = _row(close=110, sar=100, **ema_values)
    # adx=None (absent) : la modulation ne doit pas être appliquée
    df = pd.DataFrame([last, last])
    result = score_timeframe(IndicatorResult(data=df, omitted=[]), cfg_relaxed)
    assert result.category_scores["trend"] == 1.0  # sar + alignement EMA + prix>EMA200 tous +1


# =========================================================================== #
# Tiers, classification, précédence du multiplicateur d'alignement            #
# =========================================================================== #
def test_classify_thresholds(cfg):
    band = cfg.classification.neutral_band
    assert _classify(band, band) == "haussier"
    assert _classify(-band, band) == "baissier"
    assert _classify(0.0, band) == "neutre"


def test_tier_score_renormalizes_when_one_tf_missing():
    tf_scores = {"1w": TimeframeScore(s=0.5, category_scores={}, rule_outcomes=[], removed=False, omitted_indicators=[])}
    assert _tier_score(tf_scores, {"1w": 0.7, "1M": 0.3}) == pytest.approx(0.5)


def test_tier_score_none_when_all_tf_missing():
    assert _tier_score({}, {"1w": 0.7, "1M": 0.3}) is None
    removed = {"1w": TimeframeScore(s=None, category_scores={}, rule_outcomes=[], removed=True, omitted_indicators=[])}
    assert _tier_score(removed, {"1w": 0.7, "1M": 0.3}) is None


def test_alignment_multiplier_precedence_table(cfg):
    """Vérifie les 9 combinaisons (biais x référence), notamment la domination du baissier."""
    m = cfg.alignment_multiplier
    combos = {
        ("haussier", "haussier"): m.full_align,
        ("haussier", "neutre"): m.partial,
        ("haussier", "baissier"): m.contradiction,  # baissier domine malgré biais haussier
        ("neutre", "haussier"): m.partial,
        ("neutre", "neutre"): m.neutral,
        ("neutre", "baissier"): m.contradiction,
        ("baissier", "haussier"): m.contradiction,  # baissier domine malgré référence haussière
        ("baissier", "neutre"): m.contradiction,
        ("baissier", "baissier"): m.contradiction,
    }
    for (biais, reference), expected in combos.items():
        assert _alignment_multiplier(biais, reference, cfg) == expected, (biais, reference)


# =========================================================================== #
# Scénarios bout-en-bout (score_pair)                                         #
# =========================================================================== #
def _bullish_last_row(adx: float = 40.0) -> dict:
    ema_periods = _CFG.indicators.ema
    row = {f"ema_{p}": 100.0 + (len(ema_periods) - 1 - i) * 3 for i, p in enumerate(ema_periods)}
    row.update({
        "close": 110.0, "sar": 100.0,
        "volume": 100.0, "volume_sma": 50.0,
        "macd": 1.0, "macd_signal": 0.5, "macd_hist": 0.6,
        "rsi": _CFG.indicators.rsi.oversold + 1,
        "mom": 2.0, "percent_b": 0.0, "bb_width": 0.05, "adx": adx,
        "cdl_engulfing": 100, "cdl_hammer": 100, "cdl_morning_star": 100,
        "cdl_shooting_star": 0, "cdl_doji": 0,
    })
    return row


def _bullish_prev_row(adx: float = 40.0) -> dict:
    row = _bullish_last_row(adx)
    row.update({"close": 105.0, "macd_hist": 0.3, "rsi": _CFG.indicators.rsi.oversold - 1, "mom": 1.0})
    return row


def _bullish_indicator_result(adx: float = 40.0) -> IndicatorResult:
    df = pd.DataFrame([_bullish_prev_row(adx), _bullish_last_row(adx)])
    return IndicatorResult(data=df, omitted=[])


def _bearish_last_row(adx: float = 40.0) -> dict:
    ema_periods = _CFG.indicators.ema
    row = {f"ema_{p}": 100.0 - (len(ema_periods) - 1 - i) * 3 for i, p in enumerate(ema_periods)}
    row.update({
        "close": 90.0, "sar": 100.0,
        "volume": 100.0, "volume_sma": 50.0,
        "macd": -1.0, "macd_signal": -0.5, "macd_hist": -0.6,
        "rsi": _CFG.indicators.rsi.overbought - 1,
        "mom": -2.0, "percent_b": 1.0, "bb_width": 0.05, "adx": adx,
        "cdl_engulfing": -100, "cdl_hammer": 0, "cdl_morning_star": 0,
        "cdl_shooting_star": -100, "cdl_doji": 0,
    })
    return row


def _bearish_prev_row(adx: float = 40.0) -> dict:
    row = _bearish_last_row(adx)
    row.update({"close": 95.0, "macd_hist": -0.3, "rsi": _CFG.indicators.rsi.overbought + 1, "mom": -1.0})
    return row


def _bearish_indicator_result(adx: float = 40.0) -> IndicatorResult:
    df = pd.DataFrame([_bearish_prev_row(adx), _bearish_last_row(adx)])
    return IndicatorResult(data=df, omitted=[])


ALL_TFS = ("4h", "12h", "1d", "1w", "1M")


def test_score_pair_fully_bullish_scores_high(cfg_relaxed):
    tf_indicators = {tf: _bullish_indicator_result() for tf in ALL_TFS}
    result = score_pair(tf_indicators, cfg_relaxed)

    assert not result.excluded
    assert result.biais_class == "haussier"
    assert result.reference_class == "haussier"
    assert result.alignment_multiplier == cfg_relaxed.alignment_multiplier.full_align
    # < 100 exact : cdl_shooting_star (bearish-only) contribue légitimement 0 même en scénario
    # haussier, ce qui dilue un peu la moyenne de la catégorie Patterns. Score attendu très haut,
    # pas exactement 100 — sinon on masquerait un vrai 0 derrière une attente artificielle.
    assert result.score > 90.0
    assert result.level == "signal"


def test_score_pair_fully_bearish_scores_exactly_zero(cfg_relaxed):
    tf_indicators = {tf: _bearish_indicator_result() for tf in ALL_TFS}
    result = score_pair(tf_indicators, cfg_relaxed)

    assert result.declenchement_score < 0
    assert result.score == 0.0  # porte long-only : D<=0 => score=0 quel que soit m
    assert result.level == "neutre"


def test_score_pair_bearish_reference_discounts_bullish_trigger(cfg_relaxed):
    tf_indicators = {
        "4h": _bullish_indicator_result(), "12h": _bullish_indicator_result(),
        "1d": _bearish_indicator_result(),
        "1w": _bullish_indicator_result(), "1M": _bullish_indicator_result(),
    }
    result = score_pair(tf_indicators, cfg_relaxed)

    assert result.declenchement_score > 0
    assert result.reference_class == "baissier"
    assert result.biais_class == "haussier"
    assert result.alignment_multiplier == cfg_relaxed.alignment_multiplier.contradiction
    assert result.score < 30  # fortement décoté vs. ~100 dans le cas pleinement aligné


def test_score_pair_1M_missing_falls_back_to_1w_alone(cfg_relaxed):
    tf_indicators = {
        "4h": _bullish_indicator_result(), "12h": _bullish_indicator_result(),
        "1d": _bullish_indicator_result(), "1w": _bullish_indicator_result(),
    }
    result = score_pair(tf_indicators, cfg_relaxed)

    assert result.context_insufficient is False
    assert result.biais_class == "haussier"
    assert "contexte insuffisant" not in result.flags


def test_score_pair_1M_and_1w_missing_flags_context_insufficient(cfg_relaxed):
    tf_indicators = {
        "4h": _bullish_indicator_result(), "12h": _bullish_indicator_result(),
        "1d": _bullish_indicator_result(),
    }
    result = score_pair(tf_indicators, cfg_relaxed)
    full = score_pair({tf: _bullish_indicator_result() for tf in ALL_TFS}, cfg_relaxed)

    assert result.context_insufficient is True
    assert "contexte insuffisant" in result.flags
    assert result.biais_class is None
    # Biais indéterminé => traité comme neutre pour m (haussier+neutre => partial, pas full_align
    # comme dans `full` où le biais est réellement haussier) ET pénalisé séparément par
    # context_insufficient_factor : les deux effets se cumulent.
    m = cfg_relaxed.alignment_multiplier
    expected = full.score * (m.partial / m.full_align) * cfg_relaxed.context_insufficient_factor
    assert result.score == pytest.approx(expected, rel=1e-6)


def test_score_pair_excluded_when_declenchement_entirely_absent(cfg_relaxed):
    tf_indicators = {
        "1d": _bullish_indicator_result(), "1w": _bullish_indicator_result(), "1M": _bullish_indicator_result(),
    }
    result = score_pair(tf_indicators, cfg_relaxed)
    assert result.excluded is True
    assert result.exclusion_reason is not None


def test_score_pair_excluded_when_declenchement_removed_by_gate(cfg):
    short = _bullish_indicator_result()  # 2 lignes < gates.min_bars_per_tf par défaut (50)
    tf_indicators = {tf: short for tf in ALL_TFS}
    result = score_pair(tf_indicators, cfg)
    assert result.excluded is True


def test_score_timeframe_renormalizes_trend_when_ema_omitted(cfg_relaxed):
    """Une règle dont l'indicateur manque est exclue (renormalisation), jamais notée 0."""
    last = _bullish_last_row()
    prev = _bullish_prev_row()
    for p in cfg_relaxed.indicators.ema:
        last.pop(f"ema_{p}")
        prev.pop(f"ema_{p}")
    df = pd.DataFrame([prev, last])
    omitted = [f"ema_{p}" for p in cfg_relaxed.indicators.ema]

    result = score_timeframe(IndicatorResult(data=df, omitted=omitted), cfg_relaxed)
    assert result.category_scores["trend"] == 1.0  # SAR seul, bullish => pas dilué par des 0 fantômes


def test_score_timeframe_real_pipeline_n1_trend_fully_omitted_only_patterns_survive():
    """Cas réel via le Lot 2 (n=1 bougie) : trend/momentum/volatilité/volume entièrement
    omis (y compris SAR, indisponible à n=1). Seules les figures chandeliers survivent.
    Vérifie l'absence de division par zéro et de poids résiduel fantôme en conditions réelles."""
    base_cfg = load_config(CONFIG_PATH)
    relaxed_cfg = base_cfg.model_copy(
        update={"gates": base_cfg.gates.model_copy(update={"min_bars_per_tf": 1})}
    )

    open_time = pd.date_range("2020-01-01", periods=1, freq="D", tz="UTC")
    df = pd.DataFrame({
        "open_time": open_time, "open": [100.0], "high": [101.0], "low": [99.0],
        "close": [100.5], "volume": [10.0], "close_time": open_time,
    })
    indicator_result = compute_indicators(df, relaxed_cfg.indicators, relaxed_cfg.gates)
    assert "sar" in indicator_result.omitted  # confirme que trend est bien 100% omise à n=1

    result = score_timeframe(indicator_result, relaxed_cfg)

    assert not result.removed
    assert "trend" not in result.category_scores
    assert "momentum" not in result.category_scores
    assert "volatility" not in result.category_scores
    assert "volume" not in result.category_scores
    assert "patterns" in result.category_scores
    assert result.s == pytest.approx(result.category_scores["patterns"])
