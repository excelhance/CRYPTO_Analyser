"""Restitution du scan : console (rich) + CSV (§6 CDC).

Ne connaît rien du fetch/scoring : consomme uniquement un `ScanResult`
(Lot 4, `scanner.py`). Séparation stricte des couches (§1.3 CDC).
"""
from __future__ import annotations

import csv
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .config import AppConfig
from .scanner import ScanResult, ScanRow

# Convention d'affichage figée par le CDC (§6.2) : "1M"/"1w"/"1d"/"12h"/"4h" (nos clés
# internes, cf. config.intervals) -> "s_1M"/"s_1W"/"s_1D"/"s_H12"/"s_H4" (colonnes CSV).
_TF_COLUMN_SUFFIX = {"1M": "s_1M", "1w": "s_1W", "1d": "s_1D", "12h": "s_H12", "4h": "s_H4"}

CSV_COLUMNS = [
    "symbole", "score", "niveau",
    "s_1M", "s_1W", "s_1D", "s_H12", "s_H4",
    "classe_biais", "classe_reference", "multiplicateur_m", "drapeau_contexte",
    "regles_declenchees", "close", "quote_volume_24h", "atr_pct", "rsi_1d", "adx_1d",
    "horodatage",
]

_LEVEL_STYLE = {"signal": "bold green", "watch": "yellow", "neutre": "dim"}


def _format_regles_declenchees(row: ScanRow) -> str:
    """Règles scorantes à contribution non nulle, groupées par TF (§4.7 CDC)."""
    groups = []
    for tf, tf_score in row.result.timeframe_scores.items():
        triggered = [
            f"{o.rule}({o.contribution:+.2f})"
            for o in tf_score.rule_outcomes
            if o.scoring and o.contribution not in (0.0, None)
        ]
        if triggered:
            groups.append(f"{tf}:{','.join(triggered)}")
    return " | ".join(groups)


def _csv_row(row: ScanRow, horodatage: str) -> dict[str, str]:
    result = row.result
    values: dict[str, str] = {
        "symbole": row.symbol,
        "score": f"{result.score:.2f}",
        "niveau": result.level,
        "classe_biais": result.biais_class or "",
        "classe_reference": result.reference_class or "",
        "multiplicateur_m": f"{result.alignment_multiplier:.2f}",
        "drapeau_contexte": "; ".join(result.flags),  # cumule contexte insuffisant ET/OU référence 1D absente
        "regles_declenchees": _format_regles_declenchees(row),
        "close": "" if row.close is None else f"{row.close:.10g}",
        "quote_volume_24h": f"{row.quote_volume_24h:.2f}",
        "atr_pct": "" if row.atr_pct is None else f"{row.atr_pct:.6f}",
        "rsi_1d": "" if row.rsi_1d is None else f"{row.rsi_1d:.2f}",
        "adx_1d": "" if row.adx_1d is None else f"{row.adx_1d:.2f}",
        "horodatage": horodatage,
    }
    for tf, column in _TF_COLUMN_SUFFIX.items():
        tf_score = result.timeframe_scores.get(tf)
        values[column] = "" if tf_score is None or tf_score.s is None else f"{tf_score.s:.4f}"
    return values


def write_csv(scan_result: ScanResult, config: AppConfig) -> Path:
    """Écrit un CSV horodaté (une ligne par paire, triée par score décroissant, §6.2)."""
    directory = Path(config.output.directory)
    directory.mkdir(parents=True, exist_ok=True)

    timestamp = scan_result.summary.scan_timestamp
    filename = timestamp.strftime("scan_%Y%m%d_%H%M.csv")
    path = directory / filename
    horodatage = timestamp.isoformat()

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in scan_result.rows:  # déjà trié par score décroissant (scanner.run_scan)
            writer.writerow(_csv_row(row, horodatage))

    return path


def _format_flags_for_console(result) -> str:
    """Drapeaux repérables visuellement : référence 1D absente est plus grave que le
    contexte insuffisant (biais 1M/1W) — mise en avant distincte, pas juste concaténée."""
    if not result.flags:
        return "-"
    parts = []
    if result.reference_absente:
        parts.append("[bold red]⚠ reference_1d_absente[/]")
    if result.context_insufficient:
        parts.append("[yellow]contexte insuffisant[/]")
    return " ".join(parts)


def print_console_table(scan_result: ScanResult, console: Console | None = None) -> None:
    """Affiche le classement trié, coloré par niveau (§6.1 CDC)."""
    console = console or Console()
    table = Table(title="Scan Binance Spot /USDC")
    for column in ("Symbole", "Score", "Niveau", "Biais", "Référence", "Prix", "Volume 24h", "ATR%", "Drapeaux"):
        table.add_column(column)

    for row in scan_result.rows:
        result = row.result
        style = _LEVEL_STYLE.get(result.level, "")
        table.add_row(
            row.symbol,
            f"{result.score:.1f}",
            f"[{style}]{result.level}[/]" if style else result.level,
            result.biais_class or "-",
            result.reference_class or "-",
            "-" if row.close is None else f"{row.close:.10g}",
            f"{row.quote_volume_24h:,.0f}",
            "-" if row.atr_pct is None else f"{row.atr_pct:.2%}",
            _format_flags_for_console(result),
        )

    console.print(table)
    summary = scan_result.summary
    console.print(
        f"Univers : {summary.universe_size} paires | Gate D6 : {summary.qualifying_count} retenues | "
        f"Scorées : {summary.scored_count} | Exclues : {summary.excluded_count} | "
        f"Erreurs : {len(summary.failed_symbols)} | Poids consommé : {summary.total_weight_consumed}"
    )
    if summary.failed_symbols:
        console.print(f"[yellow]Paires en erreur : {', '.join(summary.failed_symbols)}[/]")
