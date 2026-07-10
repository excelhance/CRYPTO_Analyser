"""Point d'entrée en ligne de commande (Lot 0).

Commandes :
  check  — valide le fichier de configuration et affiche un résumé.
  show   — affiche la configuration normalisée (JSON).

Usage :
  python -m scanner.cli check [--config config.yaml]
  python -m scanner.cli show  [--config config.yaml]
"""
from __future__ import annotations

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console

# L'import du package déclenche `scanner/__init__.py` qui force l'encodage
# UTF-8 de stdout/stderr — nécessaire ici sous Windows quand la sortie est
# capturée/redirigée (cf. docstring de `_force_utf8_streams`).
from .config import AppConfig, load_config
from .logging_setup import setup_logging

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


if __name__ == "__main__":
    app()
