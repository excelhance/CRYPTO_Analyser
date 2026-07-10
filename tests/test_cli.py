"""Test de régression : la CLI ne doit pas planter quand sa sortie est capturée.

Sous Windows, une sortie redirigée (pipe) retombe par défaut sur l'encodage
de la locale système (souvent cp1252), qui ne sait pas encoder les
caractères Unicode affichés par `check` (✓, ≥, →, •). On force explicitement
`PYTHONIOENCODING=cp1252` pour reproduire fidèlement ce scénario, sans
dépendre du comportement du terminal réel qui lance les tests.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_check_command_succeeds_with_cp1252_stdout_and_captured_output():
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "cp1252"

    result = subprocess.run(
        [sys.executable, "-m", "scanner.cli", "check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert "UnicodeEncodeError" not in result.stderr.decode("utf-8", errors="replace")
