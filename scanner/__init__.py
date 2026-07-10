"""Scanner technique Binance Spot /USDC — outil d'aide à la décision (long uniquement)."""
from __future__ import annotations

import sys

__version__ = "0.1.0"


def _force_utf8_streams() -> None:
    """Force l'encodage UTF-8 de stdout/stderr, quel que soit le point d'entrée.

    Sous Windows, quand la sortie est capturée/redirigée (pipe, CI, outil
    externe), Python retombe sur l'encodage de la locale système (souvent
    cp1252), qui ne sait pas encoder les caractères Unicode utilisés par
    l'outil (✓, ≥, →, •, accents) : `UnicodeEncodeError`. `errors="replace"`
    est un filet de sécurité (l'affichage ne doit jamais planter), pas une
    suppression délibérée de ces caractères.

    La cause racine est le processus, pas un module en particulier : placée
    ici (chargement du package `scanner`), elle s'applique à tout point
    d'entrée — CLI, scripts manuels, futurs modules — sans duplication.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


_force_utf8_streams()
