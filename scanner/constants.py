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
