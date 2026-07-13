"""Point d'entrée en ligne de commande.

Commandes :
  check        — valide le fichier de configuration et affiche un résumé.
  show         — affiche la configuration normalisée (JSON).
  scan         — lance un scan complet (univers, gate, indicateurs, scoring, restitution).
  fundamentals — analyse fondamentale Mode B de la shortlist du dernier scan (opt-in, payant).

Usage :
  python -m scanner.cli check  [--config config.yaml]
  python -m scanner.cli show   [--config config.yaml]
  python -m scanner.cli scan   [--config config.yaml] [--force-refresh]
  python -m scanner.cli fundamentals [--config config.yaml] [--csv PATH] [--yes]
"""
from __future__ import annotations

import csv as csv_module
from pathlib import Path

import httpx
import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.prompt import Confirm

# L'import du package déclenche `scanner/__init__.py` qui force l'encodage
# UTF-8 de stdout/stderr — nécessaire ici sous Windows quand la sortie est
# capturée/redirigée (cf. docstring de `_force_utf8_streams`).
from . import fundamentals as fundamentals_module
from .config import AppConfig, load_config
from .logging_setup import setup_logging
from .rate_limiter import BinanceBannedError
from .reporting import print_console_table, write_csv, write_fundamentals_report
from .scanner import run_scan

app = typer.Typer(
    add_completion=False,
    help="Scanner technique Binance Spot /USDC — aide à la décision (long uniquement).",
)
console = Console()

DEFAULT_CONFIG = "config.yaml"


def _load_or_exit(path: str) -> AppConfig:
    """Charge la config ; en cas d'erreur, affiche un message clair et quitte (code 1)."""
    try:
        return load_config(path)
    except FileNotFoundError:
        console.print(f"[bold red]Fichier de configuration introuvable :[/] {path}")
    except yaml.YAMLError as exc:
        console.print(f"[bold red]YAML invalide[/] dans {path} :\n{exc}")
    except ValidationError as exc:
        errors = exc.errors()
        console.print(
            f"[bold red]Configuration invalide[/] ({len(errors)} erreur(s)) dans {path} :"
        )
        for err in errors:
            loc = ".".join(str(p) for p in err["loc"]) or "(racine)"
            msg = err["msg"]
            if msg.startswith("Value error, "):  # nettoie le préfixe pydantic
                msg = msg[len("Value error, "):]
            console.print(f"  [yellow]•[/] [cyan]{loc}[/] : {msg}")
    except ValueError as exc:
        console.print(f"[bold red]Configuration invalide[/] dans {path} :\n{exc}")
    raise typer.Exit(code=1)


@app.command()
def check(
    config: str = typer.Option(DEFAULT_CONFIG, "--config", "-c", help="Chemin du YAML de configuration."),
) -> None:
    """Valide le fichier de configuration et affiche un résumé."""
    log = setup_logging()
    cfg = _load_or_exit(config)
    log.info("Configuration valide : %s", config)
    console.print("[bold green]✓ Configuration valide.[/]")
    console.print(
        f"  Intervalles      : {', '.join(cfg.intervals)}\n"
        f"  Consolidation    : biais {list(cfg.tiers.biais_fond.timeframes)} → "
        f"réf. {cfg.tiers.reference.timeframe} → décl. {list(cfg.tiers.declenchement.timeframes)}\n"
        f"  Gate volume 24 h : {cfg.gates.min_quote_volume_24h:,.0f} USDC\n"
        f"  Seuils           : watch ≥ {cfg.thresholds.watch:g}, signal ≥ {cfg.thresholds.signal:g}\n"
        f"  Fondamental      : {'activé' if cfg.fundamentals.enabled else 'désactivé'} "
        f"(modèle {cfg.fundamentals.model}, top {cfg.fundamentals.top_n})"
    )


@app.command()
def show(
    config: str = typer.Option(DEFAULT_CONFIG, "--config", "-c", help="Chemin du YAML de configuration."),
) -> None:
    """Affiche la configuration normalisée (JSON)."""
    setup_logging(level="WARNING")
    cfg = _load_or_exit(config)
    console.print_json(data=cfg.model_dump(mode="json"))


@app.command()
def scan(
    config: str = typer.Option(DEFAULT_CONFIG, "--config", "-c", help="Chemin du YAML de configuration."),
    force_refresh: bool = typer.Option(
        False, "--force-refresh", help="Ignore le cache, retélécharge tout l'historique (D3)."
    ),
) -> None:
    """Lance un scan complet : univers, gate D6, indicateurs, scoring, restitution console+CSV."""
    log = setup_logging()
    cfg = _load_or_exit(config)
    if force_refresh:
        cfg = cfg.model_copy(update={"cache": cfg.cache.model_copy(update={"mode": "force_refresh"})})

    try:
        result = run_scan(cfg)
    except BinanceBannedError as exc:
        console.print(f"[bold red]Scan interrompu : {exc}[/]")
        raise typer.Exit(code=1) from exc

    print_console_table(result, console=console)
    csv_path = write_csv(result, cfg)
    log.info("CSV exporté : %s", csv_path)
    console.print(f"[bold green]CSV exporté :[/] {csv_path}")


def _latest_scan_csv(cfg: AppConfig) -> Path | None:
    """CSV de scan le plus récent dans `output.directory` (tri par date de modification)."""
    directory = Path(cfg.output.directory)
    candidates = sorted(directory.glob("scan_*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _read_shortlist_symbols(csv_path: Path, top_n: int) -> list[str]:
    """Symboles des `top_n` premières lignes d'un CSV de scan (déjà trié par score décroissant)."""
    with csv_path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv_module.DictReader(fh)
        return [row["symbole"] for row in reader][:top_n]


@app.command()
def fundamentals(
    config: str = typer.Option(DEFAULT_CONFIG, "--config", "-c", help="Chemin du YAML de configuration."),
    csv: str | None = typer.Option(
        None, "--csv", help="CSV de scan à utiliser (défaut : le plus récent dans output.directory)."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Ne pas demander de confirmation avant de dépenser."),
) -> None:
    """Analyse fondamentale Mode B (§5 CDC) de la shortlist du dernier scan.

    Jamais déclenchée automatiquement par un `scan` (coût). Affiche une estimation
    de coût avant tout appel payant et demande confirmation (sauf --yes).
    """
    log = setup_logging()
    cfg = _load_or_exit(config)

    try:
        fundamentals_module.load_environment()
        fundamentals_module.require_env(cfg.fundamentals.sources.coingecko.demo_key_env)
        fundamentals_module.require_env(cfg.fundamentals.anthropic_api_key_env)
    except fundamentals_module.FundamentalsConfigError as exc:
        console.print(f"[bold red]Configuration incomplète :[/] {exc}")
        raise typer.Exit(code=1) from exc

    csv_path = Path(csv) if csv else _latest_scan_csv(cfg)
    if csv_path is None or not csv_path.is_file():
        console.print(
            "[bold red]Aucun CSV de scan trouvé.[/] Lancez d'abord "
            "'python -m scanner.cli scan', ou précisez --csv."
        )
        raise typer.Exit(code=1)

    symbols = _read_shortlist_symbols(csv_path, cfg.fundamentals.top_n)
    if not symbols:
        console.print(f"[yellow]Shortlist vide dans {csv_path}.[/]")
        raise typer.Exit(code=0)

    console.print(f"Shortlist ({len(symbols)} paire(s), depuis {csv_path.name}) : {', '.join(symbols)}")

    import anthropic  # import différé : dépendance optionnelle, seulement nécessaire ici

    http_client = httpx.Client(timeout=20.0)
    anthropic_client = anthropic.Anthropic()
    try:
        plan = fundamentals_module.prepare_run(symbols, cfg, http_client, anthropic_client)

        pricing = cfg.fundamentals.pricing_usd_per_million_tokens
        console.print(
            f"Estimation avant appel (pire cas) :\n"
            f"  - {plan.estimated_input_tokens} tokens entrée (mesurés) × {pricing.input:.2f}$/MTok\n"
            f"  - jusqu'à {plan.estimated_max_output_tokens} tokens sortie × {pricing.output:.2f}$/MTok\n"
            f"  - jusqu'à {plan.estimated_max_web_searches} recherche(s) web × "
            f"{pricing.web_search_per_1000:.2f}$/1000 recherches\n"
            f"  ≈ [bold]{plan.estimated_cost_usd:.4f} USD[/] au total (pire cas)."
        )
        if plan.over_budget:
            console.print(
                f"[bold red]Budget dépassé :[/] "
                f"{plan.estimated_input_tokens + plan.estimated_max_output_tokens} tokens estimés > "
                f"fundamentals.max_tokens_per_run ({cfg.fundamentals.max_tokens_per_run}). "
                "Réduisez top_n ou relevez ce budget dans la config."
            )
            raise typer.Exit(code=1)

        if not yes and not Confirm.ask("Lancer les appels payants à l'API Anthropic ?", default=False):
            console.print("Annulé — aucun appel payant effectué.")
            raise typer.Exit(code=0)

        report = fundamentals_module.execute_run(plan, cfg, anthropic_client)
    finally:
        http_client.close()

    md_path, json_path = write_fundamentals_report(report, cfg)
    console.print(
        f"[bold green]Rapport fondamental exporté :[/] {md_path} (+ {json_path.name})\n"
        f"Coût réel : {report.usage.total_input_tokens} tokens entrée, "
        f"{report.usage.total_output_tokens} tokens sortie, "
        f"{report.usage.web_search_calls} recherche(s) web "
        f"≈ [bold]{report.usage.estimated_cost_usd:.4f} USD[/] au total."
    )
    log.info("Fondamental terminé : %s", md_path)


if __name__ == "__main__":
    app()
