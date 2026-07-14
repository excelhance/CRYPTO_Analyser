"""Point d'entrée en ligne de commande.

Commandes :
  check        — valide le fichier de configuration et affiche un résumé.
  show         — affiche la configuration normalisée (JSON).
  scan         — lance un scan complet (univers, gate, indicateurs, scoring, restitution).
  fundamentals — génère le prompt fondamental Mode A de la shortlist du dernier scan.

Usage :
  python -m scanner.cli check  [--config config.yaml]
  python -m scanner.cli show   [--config config.yaml]
  python -m scanner.cli scan   [--config config.yaml] [--force-refresh]
  python -m scanner.cli fundamentals [--config config.yaml] [--csv PATH]
"""
from __future__ import annotations

import csv as csv_module
from pathlib import Path

import httpx
import typer
import yaml
from pydantic import ValidationError
from rich.console import Console

# L'import du package déclenche `scanner/__init__.py` qui force l'encodage
# UTF-8 de stdout/stderr — nécessaire ici sous Windows quand la sortie est
# capturée/redirigée (cf. docstring de `_force_utf8_streams`).
from . import fundamentals as fundamentals_module
from .config import AppConfig, load_config
from .logging_setup import setup_logging
from .rate_limiter import BinanceBannedError
from .reporting import print_console_table, write_csv, write_fundamentals_prompt
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
        f"(top {cfg.fundamentals.top_n}, Mode A)"
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
) -> None:
    """Génère le prompt fondamental Mode A (§5 CDC) de la shortlist du dernier scan.

    Jamais déclenchée automatiquement par un `scan`. Ne fait aucun appel à un modèle
    de langage : récupère les données dures (CoinGecko/DefiLlama), compose un prompt
    prêt à coller dans l'interface Claude, l'écrit dans un .md et l'affiche.
    """
    log = setup_logging()
    cfg = _load_or_exit(config)

    try:
        fundamentals_module.load_environment()
        fundamentals_module.require_env(cfg.fundamentals.sources.coingecko.demo_key_env)
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

    http_client = httpx.Client(timeout=20.0)
    try:
        result = fundamentals_module.prepare_fundamentals_prompt(symbols, cfg, http_client)
    finally:
        http_client.close()

    prompt_path = write_fundamentals_prompt(result, cfg)
    console.print(f"[bold green]Prompt fondamental exporté :[/] {prompt_path}\n")
    console.print("Prompt à coller dans votre interface Claude :\n")
    console.print(result.prompt, markup=False, highlight=False)
    log.info("Prompt fondamental généré : %s", prompt_path)


if __name__ == "__main__":
    app()
