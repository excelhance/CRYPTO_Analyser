"""Test de régression : la journalisation ne doit pas planter en cp1252 capturé.

Même cause racine que test_cli.py (encodage par défaut de la locale Windows
sur une sortie redirigée), mais via un point d'entrée différent de la CLI —
`logging_setup.setup_logging()` directement — pour vérifier que le correctif
placé dans `scanner/__init__.py` s'applique bien à tout point d'entrée, sans
duplication.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Message accentué délibéré : c'est ce type de caractère qui faisait planter
# le RichHandler console en cp1252 avant le correctif.
_CODE = (
    "from scanner.logging_setup import setup_logging\n"
    "log = setup_logging()\n"
    "log.info('r\\u00e9cup\\u00e9r\\u00e9 : 3635 symboles au total')\n"
)


def test_logging_setup_handles_accented_messages_with_cp1252_stdout():
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "cp1252"

    result = subprocess.run(
        [sys.executable, "-c", _CODE],
        cwd=PROJECT_ROOT,
        capture_output=True,
        env=env,
    )

    stderr = result.stderr.decode("utf-8", errors="replace")
    assert result.returncode == 0, stderr
    assert "UnicodeEncodeError" not in stderr

    stdout = result.stdout.decode("utf-8", errors="replace")
    assert "récupéré" in stdout
