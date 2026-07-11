"""Chargement et validation de la configuration (Lot 0).

Toute la configuration de l'outil vit dans un fichier YAML unique, validé ici
par des modèles pydantic v2. Objectif : rejeter toute configuration incohérente
avec un message clair, AVANT que le moindre traitement ne démarre.

Choix de conception :
- `extra="forbid"` sur tous les modèles : une clé inconnue (faute de frappe) est
  rejetée plutôt que silencieusement ignorée.
- Validations sémantiques (somme des poids, watch < signal, fast < slow,
  intervalles cohérents…) avec messages explicites en français.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .constants import MAX_WEIGHT_PER_MINUTE, VALID_INTERVALS


# --------------------------------------------------------------------------- #
# Base commune : interdit les clés inconnues (détecte les fautes de frappe)    #
# --------------------------------------------------------------------------- #
class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Helpers de validation réutilisables                                          #
# --------------------------------------------------------------------------- #
def _ensure_valid_intervals(tfs: Iterable[str], where: str) -> None:
    """Vérifie que chaque intervalle appartient à la liste Binance valide."""
    invalid = [t for t in tfs if t not in VALID_INTERVALS]
    if invalid:
        raise ValueError(
            f"{where} : intervalle(s) invalide(s) {invalid}. "
            f"Valides : {sorted(VALID_INTERVALS)}"
        )


def _ensure_weight_map(weights: dict[str, float], where: str) -> None:
    """Vérifie qu'une pondération {intervalle: poids} est saine (∈[0,1], somme=1)."""
    _ensure_valid_intervals(weights.keys(), where)
    for tf, w in weights.items():
        if not (0.0 <= w <= 1.0):
            raise ValueError(f"{where}.{tf} : le poids doit être dans [0, 1] ; reçu {w}")
    total = sum(weights.values())
    if not math.isclose(total, 1.0, abs_tol=1e-6):
        raise ValueError(f"{where} : la somme des poids doit valoir 1.0 ; reçu {total:.4f}")


# --------------------------------------------------------------------------- #
# Univers & données                                                            #
# --------------------------------------------------------------------------- #
class UniverseCfg(_Base):
    quote_asset: str = "USDC"
    status: str = "TRADING"

    @field_validator("quote_asset")
    @classmethod
    def _upper(cls, v: str) -> str:
        v = v.strip().upper()
        if not v.isalnum():
            raise ValueError("universe.quote_asset doit être alphanumérique (ex. USDC)")
        return v


class HistoryCfg(_Base):
    limit: int = Field(ge=1, le=1000, description="Bougies par requête (max Binance = 1000).")


class CacheCfg(_Base):
    mode: Literal["incremental", "force_refresh"] = "incremental"
    directory: str = Field(min_length=1, description="Répertoire du cache parquet (relatif ou absolu).")


class GatesCfg(_Base):
    min_quote_volume_24h: float = Field(ge=0, description="Volume quote 24 h minimal (USDC).")
    min_bars_per_tf: int = Field(ge=1, description="Sous ce nombre de bougies, un TF est retiré.")
    ema200_min_bars: int = Field(ge=1, description="EMA200 calculée seulement si >= ce nombre.")
    max_level_without_reference_1d: Literal["neutre", "watch", "signal"] = Field(
        description="Niveau plafond si la référence 1D est indisponible (donnée insuffisante)."
    )


# --------------------------------------------------------------------------- #
# Rate limiter & HTTP (Lot 1)                                                  #
# --------------------------------------------------------------------------- #
class RateLimiterCfg(_Base):
    budget_per_minute: int = Field(
        gt=0, description="Budget de poids/minute du gouverneur (garde-fou anti-retry)."
    )
    max_retries: int = Field(ge=0, description="Nombre max. de tentatives après un 429.")
    backoff_base_seconds: float = Field(gt=0, description="Base du backoff (secondes) sur 429 sans Retry-After.")

    @model_validator(mode="after")
    def _check_budget_below_binance_limit(self) -> "RateLimiterCfg":
        if self.budget_per_minute >= MAX_WEIGHT_PER_MINUTE:
            raise ValueError(
                f"rate_limiter.budget_per_minute ({self.budget_per_minute}) doit être "
                f"strictement inférieur à la limite Binance ({MAX_WEIGHT_PER_MINUTE})"
            )
        return self


class HttpCfg(_Base):
    connect_timeout: float = Field(gt=0, description="Timeout de connexion (secondes).")
    read_timeout: float = Field(gt=0, description="Timeout de lecture (secondes).")
    write_timeout: float = Field(gt=0, description="Timeout d'écriture (secondes).")
    pool_timeout: float = Field(gt=0, description="Timeout d'attente d'une connexion du pool (secondes).")


# --------------------------------------------------------------------------- #
# Indicateurs                                                                  #
# --------------------------------------------------------------------------- #
class PeriodCfg(_Base):
    """Indicateur à paramètre unique (momentum, volume_ma, atr)."""
    period: int = Field(gt=0)


class RsiCfg(_Base):
    period: int = Field(gt=0)
    oversold: float = Field(gt=0, lt=100)
    overbought: float = Field(gt=0, lt=100)

    @model_validator(mode="after")
    def _check(self) -> "RsiCfg":
        if not (self.oversold < self.overbought):
            raise ValueError(
                f"rsi : 'oversold' ({self.oversold}) doit être < 'overbought' ({self.overbought})"
            )
        return self


class MacdCfg(_Base):
    fast: int = Field(gt=0)
    slow: int = Field(gt=0)
    signal: int = Field(gt=0)

    @model_validator(mode="after")
    def _check(self) -> "MacdCfg":
        if not (self.fast < self.slow):
            raise ValueError(f"macd : 'fast' ({self.fast}) doit être < 'slow' ({self.slow})")
        return self


class SarCfg(_Base):
    step: float = Field(gt=0, le=1, description="Pas d'accélération.")
    max: float = Field(gt=0, le=1, description="Accélération maximale.")

    @model_validator(mode="after")
    def _check(self) -> "SarCfg":
        if not (self.step <= self.max):
            raise ValueError(f"sar : 'step' ({self.step}) doit être ≤ 'max' ({self.max})")
        return self


class BBandsCfg(_Base):
    period: int = Field(gt=0)
    stddev: float = Field(gt=0)


class AdxCfg(_Base):
    period: int = Field(gt=0)
    trend_threshold: float = Field(gt=0, lt=100)


class IndicatorsCfg(_Base):
    ema: list[int]
    rsi: RsiCfg
    macd: MacdCfg
    sar: SarCfg
    bbands: BBandsCfg
    momentum: PeriodCfg
    volume_ma: PeriodCfg
    adx: AdxCfg
    atr: PeriodCfg

    @field_validator("ema")
    @classmethod
    def _check_ema(cls, v: list[int]) -> list[int]:
        if len(v) != 3:
            raise ValueError(
                f"indicators.ema doit contenir exactement 3 périodes "
                f"(courte, moyenne, longue) ; reçu {len(v)}"
            )
        if any(p <= 0 for p in v):
            raise ValueError("indicators.ema : toutes les périodes doivent être > 0")
        if not (v[0] < v[1] < v[2]):
            raise ValueError(
                f"indicators.ema doit être strictement croissant "
                f"(courte < moyenne < longue) ; reçu {v}"
            )
        return v


# --------------------------------------------------------------------------- #
# Scoring : poids de catégories, classification                                #
# --------------------------------------------------------------------------- #
class CategoryWeightsCfg(_Base):
    trend: float
    momentum: float
    volatility: float
    volume: float
    patterns: float

    @model_validator(mode="after")
    def _check(self) -> "CategoryWeightsCfg":
        weights = {
            "trend": self.trend,
            "momentum": self.momentum,
            "volatility": self.volatility,
            "volume": self.volume,
            "patterns": self.patterns,
        }
        for name, w in weights.items():
            if not (0.0 <= w <= 1.0):
                raise ValueError(f"category_weights.{name} doit être dans [0, 1] ; reçu {w}")
        total = sum(weights.values())
        if not math.isclose(total, 1.0, abs_tol=1e-6):
            raise ValueError(f"category_weights : la somme doit valoir 1.0 ; reçu {total:.4f}")
        return self


class ClassificationCfg(_Base):
    neutral_band: float = Field(ge=0, lt=1, description="|s| < neutral_band => neutre.")


# --------------------------------------------------------------------------- #
# Consolidation multi-échelles (tiers)                                         #
# --------------------------------------------------------------------------- #
class BiaisFondCfg(_Base):
    timeframes: dict[str, float]

    @model_validator(mode="after")
    def _check(self) -> "BiaisFondCfg":
        _ensure_weight_map(self.timeframes, "tiers.biais_fond.timeframes")
        return self


class ReferenceCfg(_Base):
    timeframe: str

    @field_validator("timeframe")
    @classmethod
    def _check(cls, v: str) -> str:
        _ensure_valid_intervals([v], "tiers.reference.timeframe")
        return v


class DeclenchementCfg(_Base):
    timeframes: dict[str, float]

    @model_validator(mode="after")
    def _check(self) -> "DeclenchementCfg":
        _ensure_weight_map(self.timeframes, "tiers.declenchement.timeframes")
        return self


class TiersCfg(_Base):
    biais_fond: BiaisFondCfg
    reference: ReferenceCfg
    declenchement: DeclenchementCfg


class AlignmentMultiplierCfg(_Base):
    full_align: float = Field(ge=0, le=1)
    partial: float = Field(ge=0, le=1)
    neutral: float = Field(ge=0, le=1)
    contradiction: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _check(self) -> "AlignmentMultiplierCfg":
        if not (self.full_align >= self.partial >= self.neutral >= self.contradiction):
            raise ValueError(
                "alignment_multiplier : l'ordre doit respecter "
                "full_align ≥ partial ≥ neutral ≥ contradiction "
                f"(reçu {self.full_align}, {self.partial}, {self.neutral}, {self.contradiction})"
            )
        return self


class ThresholdsCfg(_Base):
    watch: float = Field(ge=0, le=100)
    signal: float = Field(ge=0, le=100)

    @model_validator(mode="after")
    def _check(self) -> "ThresholdsCfg":
        if not (self.watch < self.signal):
            raise ValueError(
                f"thresholds : 'watch' ({self.watch}) doit être < 'signal' ({self.signal})"
            )
        return self


# --------------------------------------------------------------------------- #
# Fondamental (Mode B)                                                         #
# --------------------------------------------------------------------------- #
class CoingeckoCfg(_Base):
    demo_key_env: str = Field(min_length=1, description="Nom de la variable d'environnement.")


class DefillamaCfg(_Base):
    enabled: bool = True


class SourcesCfg(_Base):
    coingecko: CoingeckoCfg
    defillama: DefillamaCfg


class FundamentalsCfg(_Base):
    enabled: bool = True
    top_n: int = Field(ge=1, description="Taille de la shortlist envoyée à la synthèse.")
    model: str = Field(min_length=1)
    web_search: bool = True
    sources: SourcesCfg


# --------------------------------------------------------------------------- #
# Sortie                                                                       #
# --------------------------------------------------------------------------- #
class OutputCfg(_Base):
    format: Literal["csv"] = "csv"
    one_row_per: Literal["pair"] = "pair"
    timestamped: bool = True
    directory: str = Field(min_length=1, description="Répertoire de sortie des CSV de scan.")


# --------------------------------------------------------------------------- #
# Racine                                                                       #
# --------------------------------------------------------------------------- #
class AppConfig(_Base):
    universe: UniverseCfg
    intervals: list[str]
    history: HistoryCfg
    cache: CacheCfg
    rate_limiter: RateLimiterCfg
    http: HttpCfg
    gates: GatesCfg
    indicators: IndicatorsCfg
    category_weights: CategoryWeightsCfg
    classification: ClassificationCfg
    tiers: TiersCfg
    alignment_multiplier: AlignmentMultiplierCfg
    context_insufficient_factor: float = Field(gt=0, le=1)
    thresholds: ThresholdsCfg
    fundamentals: FundamentalsCfg
    output: OutputCfg

    @field_validator("intervals")
    @classmethod
    def _check_intervals(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("intervals : la liste ne peut pas être vide")
        if len(set(v)) != len(v):
            raise ValueError(f"intervals : doublon(s) détecté(s) dans {v}")
        _ensure_valid_intervals(v, "intervals")
        return v

    @model_validator(mode="after")
    def _check_tiers_in_intervals(self) -> "AppConfig":
        """Tout intervalle utilisé par un tier doit figurer dans 'intervals'."""
        used = (
            set(self.tiers.biais_fond.timeframes)
            | {self.tiers.reference.timeframe}
            | set(self.tiers.declenchement.timeframes)
        )
        missing = sorted(used - set(self.intervals))
        if missing:
            raise ValueError(
                f"tiers : intervalle(s) {missing} utilisé(s) mais absent(s) de "
                f"'intervals' {self.intervals}"
            )
        return self


# --------------------------------------------------------------------------- #
# Chargement                                                                   #
# --------------------------------------------------------------------------- #
def load_config(path: str | Path) -> AppConfig:
    """Charge un fichier YAML et le valide.

    Lève : FileNotFoundError, yaml.YAMLError, ValueError (fichier vide/mal formé)
    ou pydantic.ValidationError. À traiter par l'appelant (voir cli.py).
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ValueError(f"le fichier {p} ne contient pas un mapping YAML valide")
    return AppConfig.model_validate(raw)
