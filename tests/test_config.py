"""Tests de validation de la configuration.

Critère d'acceptation du Lot 0 : une configuration invalide est rejetée
(ValidationError). On vérifie aussi que la config livrée reste valide, ce qui
prémunit contre toute dérive entre `config.yaml` et le schéma.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from scanner.config import AppConfig, load_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def _base_cfg() -> dict:
    """Recharge la config livrée (source de vérité) à chaque appel."""
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_shipped_config_is_valid():
    cfg = load_config(CONFIG_PATH)
    assert cfg.intervals == ["4h", "12h", "1d", "1w", "1M"]
    assert cfg.thresholds.watch < cfg.thresholds.signal
    assert cfg.fundamentals.model == "claude-sonnet-5"


def test_invalid_interval_rejected():
    d = _base_cfg()
    d["intervals"] = ["4h", "99z"]
    with pytest.raises(ValidationError):
        AppConfig.model_validate(d)


def test_category_weights_must_sum_to_one():
    d = _base_cfg()
    d["category_weights"]["trend"] = 0.90  # somme != 1
    with pytest.raises(ValidationError):
        AppConfig.model_validate(d)


def test_watch_must_be_below_signal():
    d = _base_cfg()
    d["thresholds"] = {"watch": 80, "signal": 70}
    with pytest.raises(ValidationError):
        AppConfig.model_validate(d)


def test_macd_fast_must_be_below_slow():
    d = _base_cfg()
    d["indicators"]["macd"] = {"fast": 30, "slow": 26, "signal": 9}
    with pytest.raises(ValidationError):
        AppConfig.model_validate(d)


def test_tier_timeframe_must_exist_in_intervals():
    d = _base_cfg()
    d["intervals"] = ["4h", "12h", "1d", "1w"]  # 1M retiré, mais utilisé par biais_fond
    with pytest.raises(ValidationError):
        AppConfig.model_validate(d)


def test_tier_weights_must_sum_to_one():
    d = _base_cfg()
    d["tiers"]["declenchement"]["timeframes"] = {"4h": 0.5, "12h": 0.4}  # somme 0.9
    with pytest.raises(ValidationError):
        AppConfig.model_validate(d)


def test_ema_must_be_strictly_increasing():
    d = _base_cfg()
    d["indicators"]["ema"] = [50, 20, 200]
    with pytest.raises(ValidationError):
        AppConfig.model_validate(d)


def test_unknown_key_rejected():
    d = _base_cfg()
    d["cle_inconnue"] = 123
    with pytest.raises(ValidationError):
        AppConfig.model_validate(d)


def test_alignment_multiplier_order_enforced():
    d = _base_cfg()
    d["alignment_multiplier"]["contradiction"] = 0.9  # > neutral/partial => incohérent
    with pytest.raises(ValidationError):
        AppConfig.model_validate(d)


def test_history_limit_capped_at_1000():
    d = _base_cfg()
    d["history"]["limit"] = 5000  # dépasse le max Binance
    with pytest.raises(ValidationError):
        AppConfig.model_validate(d)
