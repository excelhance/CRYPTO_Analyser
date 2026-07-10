"""Constantes partagées du projet."""
from __future__ import annotations

# Intervalles de bougies valides sur l'API Spot de Binance (endpoint klines).
# Référence : ils évoluent rarement, mais restent vérifiables via exchangeInfo.
VALID_INTERVALS: frozenset[str] = frozenset(
    {
        "1s", "1m", "3m", "5m", "15m", "30m",
        "1h", "2h", "4h", "6h", "8h", "12h",
        "1d", "3d", "1w", "1M",
    }
)

# --------------------------------------------------------------------------- #
# Binance Spot — rate limits & poids des endpoints (§2.4 CDC)                  #
# --------------------------------------------------------------------------- #
# Limite officielle Binance : 6000 de poids/minute par IP (partagée). Le
# gouverneur (rate_limiter) doit toujours se voir configurer un budget
# STRICTEMENT INFÉRIEUR à cette valeur (marge de sécurité, cf. config.yaml).
MAX_WEIGHT_PER_MINUTE: int = 6000

# GET /api/v3/exchangeInfo — §2.1 CDC ("poids élevé, de l'ordre de 20").
WEIGHT_EXCHANGE_INFO: int = 20

# GET /api/v3/klines — §2.2 CDC.
WEIGHT_KLINES: int = 2

# GET /api/v3/ticker/24hr sans paramètre symbol/symbols (toutes les paires).
# Poids vérifié sur la documentation officielle Binance Spot API
# (developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints) :
# symbol omis => poids 80 (vs. poids 2 pour un seul symbole).
WEIGHT_TICKER_24H_ALL: int = 80
