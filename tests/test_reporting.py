"""Tests de la restitution (`reporting.py`, §6 CDC) : CSV et table console."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from scanner.config import load_config
from scanner.reporting import CSV_COLUMNS, print_console_table, write_csv
from scanner.scanner import ScanResult, ScanRow, ScanSummary
from scanner.scoring_engine import PairScoreResult, RuleOutcome, TimeframeScore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"

SCAN_TIMESTAMP = datetime(2026, 7, 12, 14, 30, tzinfo=timezone.utc)


def _scan_row(symbol: str, score: float, level: str = "watch") -> ScanRow:
    tf_scores = {
        "4h": TimeframeScore(
            s=0.5, category_scores={"trend": 1.0},
            rule_outcomes=[
                RuleOutcome("sar_position", "SAR sous le prix (support)", 1.0, True),
                RuleOutcome("squeeze", "bb_width=0.01", None, False),  # diagnostic, jamais dans regles_declenchees
                RuleOutcome("rsi_sortie_extreme", "RSI : pas de sortie de zone extrême", 0.0, True),
            ],
            removed=False, omitted_indicators=[],
        ),
        "1d": TimeframeScore(s=0.2, category_scores={}, rule_outcomes=[], removed=False, omitted_indicators=[]),
    }
    result = PairScoreResult(
        score=score, level=level, excluded=False, exclusion_reason=None,
        context_insufficient=False, reference_absente=False,
        biais_class="haussier", reference_class="neutre",
        declenchement_score=0.3, alignment_multiplier=0.7,
        timeframe_scores=tf_scores, flags=[],
    )
    return ScanRow(
        symbol=symbol, result=result, close=123.456, quote_volume_24h=2_000_000.0,
        atr_pct=0.05, rsi_1d=55.5, adx_1d=30.0,
    )


def _scan_result(rows: list[ScanRow]) -> ScanResult:
    summary = ScanSummary(
        universe_size=100, qualifying_count=len(rows), scored_count=len(rows), excluded_count=0,
        failed_symbols={}, total_weight_consumed=250, scan_timestamp=SCAN_TIMESTAMP,
    )
    return ScanResult(rows=rows, summary=summary)


def _cfg_with_output_dir(tmp_path: Path):
    cfg = load_config(CONFIG_PATH)
    return cfg.model_copy(update={"output": cfg.output.model_copy(update={"directory": str(tmp_path)})})


def test_write_csv_creates_timestamped_file_with_correct_columns(tmp_path):
    cfg = _cfg_with_output_dir(tmp_path)
    rows = [_scan_row("BUSDC", 80.0, "signal"), _scan_row("AUSDC", 20.0, "neutre")]
    scan_result = _scan_result(rows)

    path = write_csv(scan_result, cfg)

    assert path.name == "scan_20260712_1430.csv"
    assert path.parent == tmp_path
    with path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == CSV_COLUMNS
        records = list(reader)
    assert len(records) == 2


def test_write_csv_preserves_score_descending_order_of_rows(tmp_path):
    cfg = _cfg_with_output_dir(tmp_path)
    rows = [_scan_row("BUSDC", 80.0, "signal"), _scan_row("AUSDC", 20.0, "neutre")]
    scan_result = _scan_result(rows)

    path = write_csv(scan_result, cfg)
    with path.open(encoding="utf-8") as fh:
        records = list(csv.DictReader(fh))

    assert [r["symbole"] for r in records] == ["BUSDC", "AUSDC"]
    assert records[0]["score"] == "80.00"


def test_write_csv_regles_declenchees_excludes_diagnostic_and_neutral_rules(tmp_path):
    cfg = _cfg_with_output_dir(tmp_path)
    scan_result = _scan_result([_scan_row("BUSDC", 80.0)])

    path = write_csv(scan_result, cfg)
    with path.open(encoding="utf-8") as fh:
        record = next(csv.DictReader(fh))

    assert "sar_position" in record["regles_declenchees"]
    assert "squeeze" not in record["regles_declenchees"]  # non-scorante (diagnostic)
    assert "rsi_sortie_extreme" not in record["regles_declenchees"]  # contribution nulle (pas déclenchée)


def test_write_csv_creates_missing_output_directory(tmp_path):
    nested = tmp_path / "nested" / "dir"
    cfg = load_config(CONFIG_PATH)
    cfg = cfg.model_copy(update={"output": cfg.output.model_copy(update={"directory": str(nested)})})
    scan_result = _scan_result([_scan_row("AUSDC", 10.0)])

    path = write_csv(scan_result, cfg)

    assert path.exists()


def test_print_console_table_does_not_raise_on_empty_result():
    empty = _scan_result([])
    buffer = io.StringIO()
    console = Console(file=buffer, no_color=True, width=200)

    print_console_table(empty, console=console)

    assert "Univers" in buffer.getvalue()


def test_print_console_table_shows_symbol_and_score():
    scan_result = _scan_result([_scan_row("BUSDC", 80.0, "signal")])
    buffer = io.StringIO()
    console = Console(file=buffer, no_color=True, width=200)

    print_console_table(scan_result, console=console)
    output = buffer.getvalue()

    assert "BUSDC" in output
    assert "80.0" in output
