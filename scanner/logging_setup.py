"""Configuration de la journalisation : console lisible (rich) + fichier rotatif."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.logging import RichHandler


def setup_logging(level: str = "INFO", log_dir: str | Path = "logs") -> logging.Logger:
    """Configure la journalisation. Idempotent : remplace les handlers à chaque appel.

    - Console : RichHandler (couleurs, horodatage géré par rich).
    - Fichier : rotation (2 Mo × 5), format détaillé horodaté (logs/scanner.log).
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()  # évite les doublons si appelé plusieurs fois

    # Console
    console_handler = RichHandler(rich_tracebacks=True, show_path=False)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(console_handler)

    # Fichier
    file_handler = RotatingFileHandler(
        log_path / "scanner.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    )
    root.addHandler(file_handler)

    return logging.getLogger("scanner")
