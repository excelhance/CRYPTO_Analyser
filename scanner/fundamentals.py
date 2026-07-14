"""Analyse fondamentale — Mode A (§5 CDC).

Déclenché **uniquement** à la demande, sur la shortlist d'un scan (jamais tout
l'univers — les API tierces restent sollicitées avec parcimonie). Deux sources
de données dures, gratuites/quasi gratuites :
- CoinGecko (tier Demo) : catégorie/narratif, market cap, volume 24h, supply, FDV.
- DefiLlama (gratuit) : TVL, si le token est un protocole DeFi référencé.

Le script ne fait plus aucun appel à un modèle de langage : il prépare les
données et compose un **prompt unique prêt à coller** dans l'interface Claude
de l'utilisateur, qui reste dans la boucle (lecture, jugement, coût maîtrisé
par lui, pas par un estimateur). Voir docs/CDC.md §5.3 "Historique de décision"
pour les raisons de l'abandon du Mode B (appel API automatisé).

Dégradation gracieuse à chaque étage : une source en échec ou un token non
résolu ne fait jamais planter le run, seulement la partie concernée du prompt
pour ce token (le prompt le signale explicitement plutôt que d'inventer).
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv

from .config import AppConfig, CoingeckoCfg, DefillamaCfg

logger = logging.getLogger(__name__)

_DISCLAIMER = (
    "Cette synthèse est une aide à la lecture à revérifier à la source, pas une "
    "décision d'investissement (Spot long only, aucun signal short)."
)


class FundamentalsConfigError(RuntimeError):
    """Configuration incomplète pour préparer le prompt (clé API manquante, etc.)."""


# --------------------------------------------------------------------------- #
# Environnement (.env) — jamais de clé en dur, jamais loguée                   #
# --------------------------------------------------------------------------- #
def load_environment() -> None:
    """Charge `.env` dans l'environnement du processus (no-op silencieux si absent).

    N'est appelée que par la commande CLI `fundamentals` — jamais au chargement
    du package, pour qu'un `scan` normal ne dépende jamais de la présence de `.env`.
    """
    load_dotenv()


def require_env(var_name: str) -> str:
    """Lit une variable d'environnement requise ; lève une erreur claire si absente.

    Le message cite le NOM de la variable, jamais sa valeur.
    """
    value = os.environ.get(var_name)
    if not value:
        raise FundamentalsConfigError(
            f"Variable d'environnement '{var_name}' absente ou vide. "
            "Ajoutez-la dans un fichier .env à la racine du projet avant de lancer "
            "'python -m scanner.cli fundamentals'."
        )
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Modèles de données                                                           #
# --------------------------------------------------------------------------- #
@dataclass
class ResolvedToken:
    symbol: str
    coingecko_id: str
    name: str
    market_cap_rank: int | None


@dataclass
class CoingeckoMarketData:
    categories: list[str]
    market_cap_usd: float | None
    market_cap_rank: int | None
    volume_24h_usd: float | None
    circulating_supply: float | None
    total_supply: float | None
    max_supply: float | None
    fully_diluted_valuation_usd: float | None
    fetched_at: datetime


@dataclass
class TokenSnapshot:
    """Données dures rassemblées pour un token de la shortlist, avant mise en prompt."""

    symbol: str
    resolved: ResolvedToken | None
    market_data: CoingeckoMarketData | None
    tvl_usd: float | None
    errors: list[str] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=_utc_now)


@dataclass
class FundamentalsPromptResult:
    generated_at: datetime
    tokens: list[TokenSnapshot]
    prompt: str


# --------------------------------------------------------------------------- #
# CoinGecko — throttle simple (tier Demo : requests_per_minute, ex. 30/min)     #
# --------------------------------------------------------------------------- #
class CoingeckoThrottle:
    """Impose un intervalle minimal entre appels CoinGecko (pas un budget à poids : un simple espacement suffit vu le faible volume d'une shortlist)."""

    def __init__(
        self,
        requests_per_minute: int,
        sleep_func: Callable[[float], None] | None = None,
        time_func: Callable[[], float] | None = None,
    ) -> None:
        # Résolus à la construction (pas en valeur par défaut figée à l'import du
        # module) pour rester patchables par les tests (`monkeypatch` sur `time.sleep`).
        self._min_interval = 60.0 / requests_per_minute
        self._sleep = sleep_func or time.sleep
        self._time = time_func or time.monotonic
        self._last_call: float | None = None

    def wait(self) -> None:
        if self._last_call is not None:
            remaining = self._min_interval - (self._time() - self._last_call)
            if remaining > 0:
                self._sleep(remaining)
        self._last_call = self._time()


# --------------------------------------------------------------------------- #
# Résolution symbole Binance -> identifiant CoinGecko (jamais devinée)          #
# --------------------------------------------------------------------------- #
def _base_ticker(symbol: str) -> str:
    """Retire le suffixe /USDC pour obtenir le ticker de base (ex. WLDUSDC -> WLD)."""
    return symbol[: -len("USDC")] if symbol.endswith("USDC") else symbol


def resolve_coingecko_id(
    symbol: str, http_client: httpx.Client, cfg: CoingeckoCfg, api_key: str
) -> ResolvedToken | None:
    """Résout l'ID CoinGecko via `GET /search`, désambiguïsé par `market_cap_rank`.

    Ne retourne jamais un candidat au hasard : si plusieurs candidats partagent
    le même ticker et qu'aucun n'a de `market_cap_rank` connu pour trancher, le
    token est considéré **non résolu** (l'appelant le signale, ne devine pas).
    """
    base_ticker = _base_ticker(symbol)
    response = http_client.get(
        f"{cfg.base_url}/search",
        params={"query": base_ticker},
        headers={"x-cg-demo-api-key": api_key},
    )
    response.raise_for_status()
    coins = response.json().get("coins", [])
    candidates = [c for c in coins if str(c.get("symbol", "")).upper() == base_ticker.upper()]
    if not candidates:
        return None
    if len(candidates) == 1:
        best = candidates[0]
    else:
        ranked = [c for c in candidates if c.get("market_cap_rank") is not None]
        if not ranked:
            return None
        best = min(ranked, key=lambda c: c["market_cap_rank"])
    return ResolvedToken(
        symbol=symbol,
        coingecko_id=best["id"],
        name=best.get("name", ""),
        market_cap_rank=best.get("market_cap_rank"),
    )


def fetch_market_data(
    coingecko_id: str,
    http_client: httpx.Client,
    cfg: CoingeckoCfg,
    api_key: str,
    now_func: Callable[[], datetime] = _utc_now,
) -> CoingeckoMarketData:
    """Données marché/tokenomics via `GET /coins/{id}` (§5.1 CDC)."""
    response = http_client.get(
        f"{cfg.base_url}/coins/{coingecko_id}",
        params={
            "localization": "false",
            "tickers": "false",
            "community_data": "false",
            "developer_data": "false",
        },
        headers={"x-cg-demo-api-key": api_key},
    )
    response.raise_for_status()
    data = response.json()
    market_data = data.get("market_data") or {}

    def _usd(field_name: str) -> float | None:
        value = market_data.get(field_name)
        return value.get("usd") if isinstance(value, dict) else None

    return CoingeckoMarketData(
        categories=[c for c in (data.get("categories") or []) if c],
        market_cap_usd=_usd("market_cap"),
        market_cap_rank=data.get("market_cap_rank"),
        volume_24h_usd=_usd("total_volume"),
        circulating_supply=market_data.get("circulating_supply"),
        total_supply=market_data.get("total_supply"),
        max_supply=market_data.get("max_supply"),
        fully_diluted_valuation_usd=_usd("fully_diluted_valuation"),
        fetched_at=now_func(),
    )


# --------------------------------------------------------------------------- #
# DefiLlama — TVL (§5.1/§5.2 CDC)                                              #
# --------------------------------------------------------------------------- #
def fetch_defillama_protocols(http_client: httpx.Client, cfg: DefillamaCfg) -> list[dict[str, Any]]:
    """Un seul appel pour tout le run : la liste complète des protocoles DefiLlama."""
    if not cfg.enabled:
        return []
    response = http_client.get(f"{cfg.base_url}/protocols")
    response.raise_for_status()
    return response.json()


def find_tvl_by_gecko_id(protocols: list[dict[str, Any]], coingecko_id: str) -> float | None:
    """Matche par le champ `gecko_id` de DefiLlama (identique à l'ID CoinGecko)."""
    for protocol in protocols:
        if protocol.get("gecko_id") == coingecko_id:
            tvl = protocol.get("tvl")
            return float(tvl) if isinstance(tvl, (int, float)) else None
    return None


# --------------------------------------------------------------------------- #
# Construction du prompt — cadrage anti-hallucination strict (Mode A)          #
# --------------------------------------------------------------------------- #
def _fmt(value: Any) -> str:
    return "non disponible" if value is None else str(value)


def _token_hard_data_block(token: TokenSnapshot) -> str:
    lines = [f"### {token.symbol}"]
    if token.resolved is not None:
        lines.append(f"Identifiant CoinGecko : {token.resolved.coingecko_id} ({token.resolved.name})")
    else:
        lines.append("Identifiant CoinGecko : non résolu (aucun candidat fiable trouvé)")
    if token.market_data is not None:
        md = token.market_data
        lines += [
            f"Catégories/narratif : {', '.join(md.categories) or 'non disponible'}",
            f"Capitalisation (USD) : {_fmt(md.market_cap_usd)}",
            f"Rang par capitalisation : {_fmt(md.market_cap_rank)}",
            f"Volume 24h (USD) : {_fmt(md.volume_24h_usd)}",
            f"Supply circulante : {_fmt(md.circulating_supply)}",
            f"Supply totale : {_fmt(md.total_supply)}",
            f"Supply max : {_fmt(md.max_supply)}",
            f"FDV (USD) : {_fmt(md.fully_diluted_valuation_usd)}",
        ]
    else:
        lines.append("Données de marché CoinGecko : non disponibles (échec de récupération)")
    lines.append(f"TVL DefiLlama (USD) : {_fmt(token.tvl_usd)}")
    lines.append(f"Horodatage de collecte (UTC) : {token.fetched_at.isoformat()}")
    if token.errors:
        lines.append(f"Anomalies de collecte : {'; '.join(token.errors)}")
    return "\n".join(lines)


def build_shortlist_prompt(tokens: Sequence[TokenSnapshot], generated_at: datetime) -> str:
    """Compose le prompt unique couvrant toute la shortlist, prêt à coller dans Claude.

    Un seul prompt pour tous les tokens (pas un par token) : les données dures de
    chacun sont listées, suivies d'un unique bloc de consignes de sourçage et du
    format de réponse attendu (canevas Markdown, une section par token).
    """
    symbols = [t.symbol for t in tokens]
    lines = [
        "Tu prépares une revue fondamentale de plusieurs tokens crypto (Binance Spot "
        "/USDC) pour un outil d'aide à la décision technique. Ceci n'est PAS un conseil "
        "en investissement.",
        f"Horodatage de génération du prompt (UTC) : {generated_at.isoformat()}",
        f"Tokens à analyser, dans cet ordre : {', '.join(symbols)}.",
        "",
        "=== DONNÉES DURES PAR TOKEN (mesurées, datées, source CoinGecko/DefiLlama) ===",
        "",
    ]
    for token in tokens:
        lines.append(_token_hard_data_block(token))
        lines.append("")
    lines += [
        "=== CONSIGNES STRICTES (à respecter impérativement, pour CHAQUE token) ===",
        "1. Distingue toujours explicitement les DONNÉES DURES ci-dessus (chiffrées, datées) de ta "
        "SYNTHÈSE WEB (interprétation, actualité, narratif) — ne les mélange jamais dans une même "
        "affirmation sans préciser laquelle des deux tu utilises.",
        "2. Hiérarchie des sources, à respecter strictement dans tes recherches et citations : "
        "(a) PRIMAIRE — dépôt réglementaire (ex. SEC), blog officiel du projet/de la fondation, "
        "annonce officielle ; (b) RÉPUTÉE — presse spécialisée reconnue (CoinDesk, The Block, "
        "Messari, Reuters ; FR : Cryptoast, Journal du Coin) ; (c) FAIBLE OU NON VÉRIFIÉE — tout le "
        "reste. Privilégie explicitement (a) et (b) ; ne te contente jamais de (c) si un fait "
        "existe en source primaire ou réputée.",
        "3. INTERDIT de citer comme source un agrégateur généré par IA (ex. « CoinMarketCap AI "
        "Insights », « Rhea-AI » ou équivalent) : c'est un résumé automatique non audité, pas une "
        "vérification. Si tu ne trouves un fait que via un tel agrégateur, cherche la source "
        "primaire ou réputée sous-jacente ; à défaut, écris « non vérifié » plutôt que de citer "
        "l'agrégateur.",
        "4. Étiquette CHAQUE affirmation d'actualité ou de catalyseur par son niveau de fiabilité "
        "entre crochets en fin de phrase — [primaire], [réputée] ou [faible/non vérifié] — pour "
        "que le lecteur voie d'un coup d'œil ce qui est solide. Chaque affirmation DOIT aussi être "
        "sourcée par un nom de média/organisme vérifiable ou un lien précis. Une étiquette "
        "[primaire] ou [réputée] n'est valide QUE si tu peux nommer précisément la source "
        "(média/organisme) et, idéalement, fournir le lien ; une affirmation étiquetée [réputée] "
        "sans source nommable doit être rétrogradée en [faible/non vérifié].",
        "5. Toute statistique on-chain granulaire (flux, nombre d'adresses, montants précis, etc.) "
        "exige une source primaire vérifiable ; à défaut, écris « non vérifié » plutôt que de la "
        "reprendre telle quelle depuis une source secondaire ou un agrégateur.",
        "6. Traite le contexte baissier avec la même rigueur et la même profondeur que les "
        "catalyseurs positifs : performance historique du prix (ex. distance à l'ATH, tendance "
        "récente), et ampleur RÉELLE d'un catalyseur en termes de flux/montants (pas seulement son "
        "existence). 'Points de vigilance' ne doit jamais être plus court ou plus vague que "
        "'Catalyseurs' sans raison factuelle.",
        "7. N'invente JAMAIS un chiffre, une date ou un fait. Si une donnée est manquante, incertaine "
        "ou que tu ne l'as pas trouvée via ta recherche, écris explicitement « non disponible » "
        "plutôt que de la deviner, l'estimer ou la compléter.",
        f"8. Rappelle-toi et rappelle dans chaque résumé que {_DISCLAIMER}",
        "9. L'aveu d'ignorance est explicitement AUTORISÉ et EXIGÉ : si tu ne trouves pas de source "
        "primaire ou réputée pour étayer une affirmation, ne l'inclus pas — ou inscris explicitement "
        "« aucune source fiable trouvée ». Ne comble JAMAIS un manque par une source faible "
        "étiquetée à tort, ni par une affirmation plausible non vérifiée. Une section courte parce "
        "que l'information fiable manque est préférable à une section remplie de suppositions.",
        "",
        "=== FORMAT DE RÉPONSE ATTENDU ===",
        "Réponds en Markdown, avec EXACTEMENT une section par token (même ordre que ci-dessus), "
        "suivant ce canevas :",
        "",
        "## {SYMBOLE}",
        "",
        "**Résumé :** ...",
        "",
        "**Points positifs :**",
        "- ... [primaire/réputée/faible/non vérifié]",
        "",
        "**Points de vigilance :**",
        "- ... [primaire/réputée/faible/non vérifié]",
        "",
        "**Catalyseurs :**",
        "- ... [primaire/réputée/faible/non vérifié]",
        "",
        "**Sources :**",
        "- Nom (primaire/réputée/faible) — lien si disponible",
        "",
        "**Date des données :** ...",
        "",
        f"Termine ta réponse par ce rappel, une seule fois : {_DISCLAIMER}",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Préparation du prompt (aucun appel payant, aucune clé Anthropic requise)      #
# --------------------------------------------------------------------------- #
def prepare_fundamentals_prompt(
    symbols: Sequence[str],
    config: AppConfig,
    http_client: httpx.Client,
    now_func: Callable[[], datetime] = _utc_now,
) -> FundamentalsPromptResult:
    """Résout, enrichit et compose le prompt unique de la shortlist (Mode A).

    Aucun appel à un modèle de langage ici : uniquement CoinGecko/DefiLlama.
    """
    fcfg = config.fundamentals
    cg_key = require_env(fcfg.sources.coingecko.demo_key_env)
    throttle = CoingeckoThrottle(fcfg.sources.coingecko.requests_per_minute)

    protocols: list[dict[str, Any]] = []
    if fcfg.sources.defillama.enabled:
        try:
            protocols = fetch_defillama_protocols(http_client, fcfg.sources.defillama)
        except httpx.HTTPError as exc:
            logger.warning("fundamentals : DefiLlama injoignable (%r), TVL indisponible pour ce run", exc)

    tokens: list[TokenSnapshot] = []
    for symbol in symbols:
        errors: list[str] = []
        resolved: ResolvedToken | None = None
        market_data: CoingeckoMarketData | None = None
        tvl_usd: float | None = None

        throttle.wait()
        try:
            resolved = resolve_coingecko_id(symbol, http_client, fcfg.sources.coingecko, cg_key)
            if resolved is None:
                errors.append("Identifiant CoinGecko non résolu (aucun candidat fiable)")
                logger.warning("fundamentals : %s non résolu sur CoinGecko", symbol)
        except httpx.HTTPError as exc:
            errors.append(f"Résolution CoinGecko en échec : {exc!r}")
            logger.warning("fundamentals : résolution CoinGecko en échec pour %s : %r", symbol, exc)

        if resolved is not None:
            throttle.wait()
            try:
                market_data = fetch_market_data(
                    resolved.coingecko_id, http_client, fcfg.sources.coingecko, cg_key, now_func
                )
            except httpx.HTTPError as exc:
                errors.append(f"Données de marché CoinGecko en échec : {exc!r}")
                logger.warning("fundamentals : market data en échec pour %s : %r", symbol, exc)
            tvl_usd = find_tvl_by_gecko_id(protocols, resolved.coingecko_id)

        tokens.append(
            TokenSnapshot(
                symbol=symbol,
                resolved=resolved,
                market_data=market_data,
                tvl_usd=tvl_usd,
                errors=errors,
                fetched_at=now_func(),
            )
        )

    generated_at = now_func()
    prompt = build_shortlist_prompt(tokens, generated_at)
    return FundamentalsPromptResult(generated_at=generated_at, tokens=tokens, prompt=prompt)
