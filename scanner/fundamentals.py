"""Analyse fondamentale — Mode B (§5 CDC).

Déclenché **uniquement** à la demande, sur la shortlist d'un scan (jamais tout
l'univers — les API tierces et l'API Anthropic sont payantes). Trois sources :
- CoinGecko (tier Demo) : catégorie/narratif, market cap, volume 24h, supply, FDV.
- DefiLlama (gratuit) : TVL, si le token est un protocole DeFi référencé.
- Recherche web de Claude (`claude-sonnet-5`) : actualité/narratif FR+EN, synthèse JSON.

Architecture en deux temps, pour ne jamais dépenser sans confirmation explicite :
- `prepare_run` : résout les tokens, récupère les données de marché (peu coûteux),
  construit les prompts et mesure leur coût réel via `count_tokens` (gratuit) ->
  produit un `FundamentalsRunPlan` avec une estimation de coût AVANT tout appel payant.
- `execute_run` : consomme le plan et déclenche les appels de synthèse Claude.

Dégradation gracieuse à chaque étage : une source en échec ou un token non résolu
ne fait jamais planter le run, seulement la partie concernée de la synthèse.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx
from dotenv import load_dotenv

from .config import AppConfig, CoingeckoCfg, DefillamaCfg, FundamentalsCfg

if TYPE_CHECKING:
    import anthropic

logger = logging.getLogger(__name__)

_DISCLAIMER = (
    "Cette synthèse est une aide à la lecture à revérifier à la source, pas une "
    "décision d'investissement (Spot long only, aucun signal short)."
)


class FundamentalsConfigError(RuntimeError):
    """Configuration incomplète pour lancer le fondamental (clé API manquante, etc.)."""


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
class LlmSynthesis:
    resume: str | None
    points_positifs: list[str]
    points_vigilance: list[str]
    catalyseurs: list[str]
    sources: list[str]
    date_donnees: str | None
    raw_text: str


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    web_search_calls: int = 0


@dataclass
class TokenPlan:
    """Données rassemblées pour un token, avant le déclenchement de l'appel payant."""

    symbol: str
    resolved: ResolvedToken | None
    market_data: CoingeckoMarketData | None
    tvl_usd: float | None
    prompt: str
    estimated_input_tokens: int
    errors: list[str] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=_utc_now)


@dataclass
class FundamentalsRunPlan:
    tokens: list[TokenPlan]
    estimated_input_tokens: int
    estimated_max_output_tokens: int
    estimated_max_web_searches: int
    estimated_cost_usd: float
    over_budget: bool


@dataclass
class TokenFundamentals:
    symbol: str
    resolved: ResolvedToken | None
    market_data: CoingeckoMarketData | None
    tvl_usd: float | None
    synthesis: LlmSynthesis | None
    usage: TokenUsage
    errors: list[str]
    fetched_at: datetime


@dataclass
class RunUsageSummary:
    total_input_tokens: int
    total_output_tokens: int
    estimated_cost_usd: float
    web_search_calls: int


@dataclass
class FundamentalsReport:
    generated_at: datetime
    tokens: list[TokenFundamentals]
    usage: RunUsageSummary


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
# Construction du prompt — cadrage anti-hallucination strict                   #
# --------------------------------------------------------------------------- #
def _fmt(value: Any) -> str:
    return "non disponible" if value is None else str(value)


def build_synthesis_prompt(
    symbol: str,
    resolved: ResolvedToken | None,
    market_data: CoingeckoMarketData | None,
    tvl_usd: float | None,
    fetched_at: datetime,
) -> str:
    lines = [
        f"Tu analyses le token crypto {symbol} (Binance Spot /USDC) pour un outil d'aide à la "
        "décision technique. Ceci n'est PAS un conseil en investissement.",
        "",
        "=== DONNÉES DURES (mesurées, datées, source CoinGecko/DefiLlama) ===",
        f"Horodatage de collecte (UTC) : {fetched_at.isoformat()}",
    ]
    if resolved is not None:
        lines.append(f"Identifiant CoinGecko : {resolved.coingecko_id} ({resolved.name})")
    else:
        lines.append("Identifiant CoinGecko : non résolu (aucun candidat fiable trouvé)")
    if market_data is not None:
        lines += [
            f"Catégories/narratif : {', '.join(market_data.categories) or 'non disponible'}",
            f"Capitalisation (USD) : {_fmt(market_data.market_cap_usd)}",
            f"Rang par capitalisation : {_fmt(market_data.market_cap_rank)}",
            f"Volume 24h (USD) : {_fmt(market_data.volume_24h_usd)}",
            f"Supply circulante : {_fmt(market_data.circulating_supply)}",
            f"Supply totale : {_fmt(market_data.total_supply)}",
            f"Supply max : {_fmt(market_data.max_supply)}",
            f"FDV (USD) : {_fmt(market_data.fully_diluted_valuation_usd)}",
        ]
    else:
        lines.append("Données de marché CoinGecko : non disponibles (échec de récupération)")
    lines.append(f"TVL DefiLlama (USD) : {_fmt(tvl_usd)}")
    lines += [
        "",
        "=== CONSIGNES STRICTES (à respecter impérativement) ===",
        "1. Distingue toujours explicitement les DONNÉES DURES ci-dessus (chiffrées, datées) de ta "
        "SYNTHÈSE WEB (interprétation, actualité, narratif) — ne les mélange jamais dans une même "
        "affirmation sans préciser laquelle des deux tu utilises.",
        "2. Utilise l'outil de recherche web pour l'actualité récente (sources FR : Cryptoast, Journal "
        "du Coin ; sources EN : CoinDesk, The Block, Messari). Chaque affirmation d'actualité ou de "
        "catalyseur DOIT être sourcée par un nom de média vérifiable ou un lien précis — aucune "
        "affirmation d'actualité non sourcée.",
        "3. N'invente JAMAIS un chiffre, une date ou un fait. Si une donnée est manquante, incertaine "
        "ou que tu ne l'as pas trouvée via la recherche web, écris explicitement « non disponible » "
        "plutôt que de la deviner, l'estimer ou la compléter.",
        f"4. Rappelle-toi et rappelle dans 'resume' que {_DISCLAIMER}",
        "",
        "Réponds UNIQUEMENT avec un objet JSON valide (aucun texte avant/après, aucun délimiteur ```), "
        "avec exactement ces clés :",
        '{"resume": "...", "points_positifs": ["..."], "points_vigilance": ["..."], '
        '"catalyseurs": ["..."], "sources": ["..."], "date_donnees": "..."}',
    ]
    return "\n".join(lines)


def _web_search_tool(cfg: FundamentalsCfg) -> dict[str, Any]:
    return {"type": "web_search_20260209", "name": "web_search", "max_uses": cfg.web_search_max_uses}


# --------------------------------------------------------------------------- #
# Parsing défensif de la sortie LLM (§5.3 CDC)                                 #
# --------------------------------------------------------------------------- #
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def parse_llm_json(raw_text: str) -> dict[str, Any]:
    """Extrait un objet JSON d'une réponse LLM (délimiteurs ``` et/ou préambule tolérés).

    Lève `ValueError` si aucun JSON exploitable n'a pu être extrait — à charge de
    l'appelant de marquer la synthèse indisponible plutôt que de propager l'erreur.
    """
    text = raw_text.strip()
    fence_match = _JSON_FENCE_RE.search(text)
    if fence_match:
        text = fence_match.group(1).strip()
    else:
        start = text.find("{")
        if start == -1:
            raise ValueError("aucun objet JSON trouvé dans la réponse du modèle")
        depth = 0
        end = None
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is None:
            raise ValueError("objet JSON non terminé dans la réponse du modèle")
        text = text[start:end]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"JSON extrait n'est pas un objet ({type(parsed).__name__})")
    return parsed


def _build_synthesis(parsed: dict[str, Any], raw_text: str) -> LlmSynthesis:
    def _list(key: str) -> list[str]:
        value = parsed.get(key) or []
        return [str(v) for v in value] if isinstance(value, list) else []

    return LlmSynthesis(
        resume=parsed.get("resume"),
        points_positifs=_list("points_positifs"),
        points_vigilance=_list("points_vigilance"),
        catalyseurs=_list("catalyseurs"),
        sources=_list("sources"),
        date_donnees=parsed.get("date_donnees"),
        raw_text=raw_text,
    )


# --------------------------------------------------------------------------- #
# Appel de synthèse Claude (avec gestion de pause_turn — boucle recherche web)  #
# --------------------------------------------------------------------------- #
def _extract_text(content: list[Any]) -> str:
    return "".join(getattr(block, "text", "") for block in content if getattr(block, "type", None) == "text")


def _count_web_search_calls(content: list[Any]) -> int:
    return sum(
        1
        for block in content
        if getattr(block, "type", None) == "server_tool_use" and getattr(block, "name", None) == "web_search"
    )


def call_claude_synthesis(
    anthropic_client: "anthropic.Anthropic", cfg: FundamentalsCfg, prompt: str
) -> tuple[str, TokenUsage]:
    """Appelle Claude avec l'outil de recherche web ; relance sur `pause_turn` (bornée)."""
    tools = [_web_search_tool(cfg)] if cfg.web_search else []
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    usage = TokenUsage()
    response = None
    for _ in range(cfg.max_continuations + 1):
        response = anthropic_client.messages.create(
            model=cfg.model,
            max_tokens=cfg.max_tokens_per_call,
            tools=tools,
            messages=messages,
        )
        usage.input_tokens += response.usage.input_tokens
        usage.output_tokens += response.usage.output_tokens
        usage.web_search_calls += _count_web_search_calls(response.content)
        if response.stop_reason != "pause_turn":
            break
        messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": response.content},
        ]
    return _extract_text(response.content), usage


# --------------------------------------------------------------------------- #
# Phase 1 — préparation (données + estimation de coût, AUCUN appel payant)     #
# --------------------------------------------------------------------------- #
def prepare_run(
    symbols: Sequence[str],
    config: AppConfig,
    http_client: httpx.Client,
    anthropic_client: "anthropic.Anthropic",
    now_func: Callable[[], datetime] = _utc_now,
) -> FundamentalsRunPlan:
    """Résout, enrichit et construit les prompts — mesure le coût réel via `count_tokens`
    (endpoint non facturé en génération) pour produire une estimation AVANT tout appel payant.
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

    token_plans: list[TokenPlan] = []
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

        fetched_at = now_func()
        prompt = build_synthesis_prompt(symbol, resolved, market_data, tvl_usd, fetched_at)
        counted = anthropic_client.messages.count_tokens(
            model=fcfg.model, messages=[{"role": "user", "content": prompt}]
        )
        token_plans.append(
            TokenPlan(
                symbol=symbol,
                resolved=resolved,
                market_data=market_data,
                tvl_usd=tvl_usd,
                prompt=prompt,
                estimated_input_tokens=counted.input_tokens,
                errors=errors,
                fetched_at=fetched_at,
            )
        )

    total_input = sum(t.estimated_input_tokens for t in token_plans)
    worst_case_output = len(token_plans) * fcfg.max_tokens_per_call
    # Pire cas : chaque token consomme le nombre max. de recherches web autorisé
    # (`web_search_max_uses`). Tarif plat vérifié ($10/1000 recherches, indépendant
    # du modèle) — jamais laissé « non chiffré » dans l'estimation présentée à l'utilisateur.
    worst_case_web_searches = len(token_plans) * fcfg.web_search_max_uses if fcfg.web_search else 0
    pricing = fcfg.pricing_usd_per_million_tokens
    estimated_cost = (
        total_input / 1_000_000 * pricing.input
        + worst_case_output / 1_000_000 * pricing.output
        + worst_case_web_searches / 1000 * pricing.web_search_per_1000
    )
    over_budget = (total_input + worst_case_output) > fcfg.max_tokens_per_run
    return FundamentalsRunPlan(
        tokens=token_plans,
        estimated_input_tokens=total_input,
        estimated_max_output_tokens=worst_case_output,
        estimated_max_web_searches=worst_case_web_searches,
        estimated_cost_usd=estimated_cost,
        over_budget=over_budget,
    )


# --------------------------------------------------------------------------- #
# Phase 2 — exécution (appels payants, après confirmation côté CLI)            #
# --------------------------------------------------------------------------- #
def execute_run(
    plan: FundamentalsRunPlan,
    config: AppConfig,
    anthropic_client: "anthropic.Anthropic",
    now_func: Callable[[], datetime] = _utc_now,
) -> FundamentalsReport:
    fcfg = config.fundamentals
    pricing = fcfg.pricing_usd_per_million_tokens
    tokens: list[TokenFundamentals] = []
    total_input = total_output = total_web_search = 0

    for token_plan in plan.tokens:
        errors = list(token_plan.errors)
        synthesis: LlmSynthesis | None = None
        usage = TokenUsage()
        try:
            raw_text, usage = call_claude_synthesis(anthropic_client, fcfg, token_plan.prompt)
            try:
                parsed = parse_llm_json(raw_text)
                synthesis = _build_synthesis(parsed, raw_text)
            except (ValueError, json.JSONDecodeError) as exc:
                errors.append(f"Synthèse LLM non parsable : {exc!r}")
                logger.error("fundamentals : parsing JSON en échec pour %s : %r", token_plan.symbol, exc)
        except Exception as exc:  # noqa: BLE001 - un échec Claude ne doit jamais tuer le run
            errors.append(f"Appel de synthèse Claude en échec : {exc!r}")
            logger.error("fundamentals : appel Claude en échec pour %s : %r", token_plan.symbol, exc)

        total_input += usage.input_tokens
        total_output += usage.output_tokens
        total_web_search += usage.web_search_calls

        tokens.append(
            TokenFundamentals(
                symbol=token_plan.symbol,
                resolved=token_plan.resolved,
                market_data=token_plan.market_data,
                tvl_usd=token_plan.tvl_usd,
                synthesis=synthesis,
                usage=usage,
                errors=errors,
                fetched_at=token_plan.fetched_at,
            )
        )

    real_cost = (
        total_input / 1_000_000 * pricing.input
        + total_output / 1_000_000 * pricing.output
        + total_web_search / 1000 * pricing.web_search_per_1000
    )
    summary = RunUsageSummary(
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        estimated_cost_usd=real_cost,
        web_search_calls=total_web_search,
    )
    return FundamentalsReport(generated_at=now_func(), tokens=tokens, usage=summary)
