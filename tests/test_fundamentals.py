"""Tests de `fundamentals` (Lot 5, §5 CDC, Mode A) : résolution CoinGecko, récupération des
données dures, dégradation gracieuse, gestion de l'environnement, et composition du prompt.

Aucun appel réseau réel : CoinGecko/DefiLlama sont mockés via `httpx.MockTransport`. Aucun
appel à un modèle de langage n'existe plus dans ce module (Mode A : le prompt est généré
pour être collé manuellement dans l'interface Claude) — aucune clé Anthropic n'est requise.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from scanner import fundamentals as f
from scanner.config import load_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"

NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


def _now() -> datetime:
    return NOW


@pytest.fixture
def cfg():
    return load_config(CONFIG_PATH)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Neutralise le throttle CoinGecko (2s/appel à 30 req/min) : inutile en test."""
    monkeypatch.setattr(f.time, "sleep", lambda _seconds: None)


@pytest.fixture
def coingecko_key(monkeypatch):
    monkeypatch.setenv("COINGECKO_DEMO_KEY", "CG-test-key")
    return "CG-test-key"


# --------------------------------------------------------------------------- #
# Environnement (.env) — jamais de crash, message clair, aucune clé loguée     #
# --------------------------------------------------------------------------- #
def test_require_env_missing_raises_clear_error(monkeypatch):
    monkeypatch.delenv("SOME_MISSING_KEY", raising=False)
    with pytest.raises(f.FundamentalsConfigError) as exc_info:
        f.require_env("SOME_MISSING_KEY")
    assert "SOME_MISSING_KEY" in str(exc_info.value)


def test_require_env_present(monkeypatch):
    monkeypatch.setenv("SOME_KEY", "secret-value")
    assert f.require_env("SOME_KEY") == "secret-value"


def test_load_environment_without_dotenv_file_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # aucun fichier .env dans ce répertoire
    f.load_environment()  # ne doit lever aucune exception


# --------------------------------------------------------------------------- #
# Throttle CoinGecko — pas d'attente réelle en test (monkeypatch de time.sleep) #
# --------------------------------------------------------------------------- #
def test_coingecko_throttle_waits_between_close_calls(monkeypatch):
    """Deux appels rapprochés doivent déclencher une attente ; `time.sleep` est mocké."""
    fake_clock = {"now": 0.0}
    sleep_calls: list[float] = []

    def fake_time() -> float:
        return fake_clock["now"]

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        fake_clock["now"] += seconds

    throttle = f.CoingeckoThrottle(requests_per_minute=30, sleep_func=fake_sleep, time_func=fake_time)
    throttle.wait()  # premier appel : jamais d'attente
    throttle.wait()  # immédiatement après : doit attendre ~2s (60/30)
    assert sleep_calls == [2.0]


def test_coingecko_throttle_uses_real_time_sleep_by_default(monkeypatch):
    """Sans injection explicite, le throttle résout `time.sleep` dynamiquement (patchable)."""
    calls: list[float] = []
    monkeypatch.setattr(f.time, "sleep", lambda seconds: calls.append(seconds))
    monkeypatch.setattr(f.time, "monotonic", lambda: 0.0)

    throttle = f.CoingeckoThrottle(requests_per_minute=30)
    throttle.wait()
    throttle.wait()
    assert calls == [2.0]


# --------------------------------------------------------------------------- #
# Résolution symbole -> ID CoinGecko (jamais devinée)                          #
# --------------------------------------------------------------------------- #
def _search_response(coins: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"coins": coins})


def test_resolve_single_candidate(cfg, coingecko_key):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-cg-demo-api-key"] == coingecko_key
        assert request.url.params["query"] == "WLD"
        return _search_response([{"id": "worldcoin", "name": "Worldcoin", "symbol": "wld", "market_cap_rank": 120}])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolved = f.resolve_coingecko_id("WLDUSDC", client, cfg.fundamentals.sources.coingecko, coingecko_key)
    assert resolved is not None
    assert resolved.coingecko_id == "worldcoin"
    assert resolved.symbol == "WLDUSDC"
    assert resolved.market_cap_rank == 120


def test_resolve_disambiguates_by_market_cap_rank(cfg, coingecko_key):
    """Ticker non-unique : le candidat au rang de capitalisation le plus bas gagne."""
    def handler(request: httpx.Request) -> httpx.Response:
        return _search_response(
            [
                {"id": "obscure-fork-token", "name": "Obscure Fork", "symbol": "sol", "market_cap_rank": 4200},
                {"id": "solana", "name": "Solana", "symbol": "sol", "market_cap_rank": 5},
            ]
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolved = f.resolve_coingecko_id("SOLUSDC", client, cfg.fundamentals.sources.coingecko, coingecko_key)
    assert resolved is not None
    assert resolved.coingecko_id == "solana"


def test_resolve_ambiguous_without_rank_is_unresolved(cfg, coingecko_key):
    """Plusieurs candidats, aucun rang pour trancher => non résolu (jamais un choix au hasard)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return _search_response(
            [
                {"id": "token-a", "name": "Token A", "symbol": "xyz", "market_cap_rank": None},
                {"id": "token-b", "name": "Token B", "symbol": "xyz", "market_cap_rank": None},
            ]
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolved = f.resolve_coingecko_id("XYZUSDC", client, cfg.fundamentals.sources.coingecko, coingecko_key)
    assert resolved is None


def test_resolve_no_candidate_is_unresolved(cfg, coingecko_key):
    def handler(request: httpx.Request) -> httpx.Response:
        return _search_response([])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolved = f.resolve_coingecko_id("GHOSTUSDC", client, cfg.fundamentals.sources.coingecko, coingecko_key)
    assert resolved is None


def test_resolve_ignores_ticker_mismatch(cfg, coingecko_key):
    """Un résultat de recherche floue dont le ticker ne correspond pas exactement est écarté."""
    def handler(request: httpx.Request) -> httpx.Response:
        return _search_response([{"id": "unrelated", "name": "Unrelated", "symbol": "abc", "market_cap_rank": 1}])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolved = f.resolve_coingecko_id("WLDUSDC", client, cfg.fundamentals.sources.coingecko, coingecko_key)
    assert resolved is None


# --------------------------------------------------------------------------- #
# Données de marché CoinGecko                                                  #
# --------------------------------------------------------------------------- #
def test_fetch_market_data_parses_fields(cfg, coingecko_key):
    coin_detail = {
        "categories": ["Layer 1", None, "Smart Contract Platform"],
        "market_cap_rank": 5,
        "market_data": {
            "market_cap": {"usd": 123456.0},
            "total_volume": {"usd": 7890.0},
            "fully_diluted_valuation": {"usd": 999999.0},
            "circulating_supply": 100.0,
            "total_supply": 200.0,
            "max_supply": 300.0,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/coins/solana"
        return httpx.Response(200, json=coin_detail)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    data = f.fetch_market_data("solana", client, cfg.fundamentals.sources.coingecko, coingecko_key, now_func=_now)
    assert data.categories == ["Layer 1", "Smart Contract Platform"]
    assert data.market_cap_usd == 123456.0
    assert data.market_cap_rank == 5
    assert data.volume_24h_usd == 7890.0
    assert data.fully_diluted_valuation_usd == 999999.0
    assert data.circulating_supply == 100.0
    assert data.fetched_at == NOW


# --------------------------------------------------------------------------- #
# DefiLlama — TVL par gecko_id                                                 #
# --------------------------------------------------------------------------- #
def test_find_tvl_by_gecko_id_matches():
    protocols = [
        {"gecko_id": "aave", "tvl": 12345.6},
        {"gecko_id": "solana", "tvl": 999.0},
    ]
    assert f.find_tvl_by_gecko_id(protocols, "aave") == 12345.6


def test_find_tvl_by_gecko_id_no_match_returns_none():
    protocols = [{"gecko_id": "aave", "tvl": 12345.6}]
    assert f.find_tvl_by_gecko_id(protocols, "some-non-defi-token") is None


# --------------------------------------------------------------------------- #
# prepare_fundamentals_prompt — dégradation gracieuse (CoinGecko en échec)     #
# --------------------------------------------------------------------------- #
def test_prepare_prompt_degrades_gracefully_on_coingecko_failure(cfg, coingecko_key):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/search":
            return httpx.Response(500, text="internal error")
        if request.url.path == "/protocols":
            return httpx.Response(200, json=[])
        raise AssertionError(f"URL inattendue : {request.url}")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))

    result = f.prepare_fundamentals_prompt(["ABCUSDC"], cfg, http_client, now_func=_now)

    assert len(result.tokens) == 1
    token = result.tokens[0]
    assert token.resolved is None
    assert token.market_data is None
    assert any("CoinGecko" in err for err in token.errors)
    # Le run continue malgré l'échec : un prompt est bien composé, qui signale l'anomalie.
    assert "ABCUSDC" in result.prompt
    assert "non résolu" in result.prompt or "échec" in result.prompt


def test_prepare_prompt_unresolved_token_is_logged_not_guessed(cfg, coingecko_key):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/search":
            return httpx.Response(200, json={"coins": []})
        if request.url.path == "/protocols":
            return httpx.Response(200, json=[])
        raise AssertionError(f"URL inattendue : {request.url}")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))

    result = f.prepare_fundamentals_prompt(["GHOSTUSDC"], cfg, http_client, now_func=_now)
    token = result.tokens[0]
    assert token.resolved is None
    assert "non résolu" in token.errors[0]


# --------------------------------------------------------------------------- #
# build_shortlist_prompt — le prompt doit porter les données dures + le cadrage #
# --------------------------------------------------------------------------- #
def _snapshot(symbol: str, market_cap: float = 123456.0) -> f.TokenSnapshot:
    resolved = f.ResolvedToken(symbol=symbol, coingecko_id=symbol.lower(), name=symbol, market_cap_rank=42)
    market_data = f.CoingeckoMarketData(
        categories=["Layer 1"],
        market_cap_usd=market_cap,
        market_cap_rank=42,
        volume_24h_usd=7890.0,
        circulating_supply=100.0,
        total_supply=200.0,
        max_supply=300.0,
        fully_diluted_valuation_usd=999999.0,
        fetched_at=NOW,
    )
    return f.TokenSnapshot(
        symbol=symbol, resolved=resolved, market_data=market_data, tvl_usd=42.0, errors=[], fetched_at=NOW
    )


def test_build_shortlist_prompt_contains_hard_data_for_every_token():
    tokens = [_snapshot("WLDUSDC", market_cap=1_000_000.0), _snapshot("BTCUSDC", market_cap=2_000_000.0)]
    prompt = f.build_shortlist_prompt(tokens, NOW)

    for symbol in ("WLDUSDC", "BTCUSDC"):
        assert symbol in prompt
    assert "1000000.0" in prompt or "1000000" in prompt.replace(",", "")
    assert "999999.0" in prompt  # FDV
    assert "42.0" in prompt  # TVL


def test_build_shortlist_prompt_signals_missing_data_without_inventing():
    incomplete = f.TokenSnapshot(
        symbol="GHOSTUSDC", resolved=None, market_data=None, tvl_usd=None,
        errors=["Identifiant CoinGecko non résolu (aucun candidat fiable)"], fetched_at=NOW,
    )
    prompt = f.build_shortlist_prompt([incomplete], NOW)
    assert "GHOSTUSDC" in prompt
    assert "non résolu" in prompt
    assert "non disponible" in prompt


def test_build_shortlist_prompt_contains_sourcing_guidance():
    """Le cadrage anti-hallucination et la hiérarchie de sources doivent être présents
    dans le prompt (ils ne sont plus imposés côté code puisqu'il n'y a plus d'appel API)."""
    prompt = f.build_shortlist_prompt([_snapshot("WLDUSDC")], NOW)

    # Interdiction des agrégateurs générés par IA.
    assert "CoinMarketCap AI Insights" in prompt
    assert "INTERDIT" in prompt

    # Hiérarchie de sources et étiquetage de fiabilité.
    assert "PRIMAIRE" in prompt
    assert "RÉPUTÉE" in prompt
    assert "CoinDesk" in prompt
    assert "Cryptoast" in prompt
    assert "[primaire]" in prompt
    assert "[réputée]" in prompt

    # On-chain granulaire sans source primaire => non vérifié.
    assert "non vérifié" in prompt

    # Étiquette [primaire]/[réputée] valide seulement si la source est nommable ; sinon rétrogradée.
    assert "n'est valide QUE si tu peux nommer précisément la source" in prompt
    assert "rétrogradée en [faible/non vérifié]" in prompt

    # Aveu d'ignorance explicitement autorisé et exigé (jamais de source faible étiquetée à tort).
    assert "aveu d'ignorance" in prompt.lower()
    assert "aucune source fiable trouvée" in prompt
    assert "AUTORISÉ" in prompt and "EXIGÉ" in prompt

    # Parité de traitement baissier/haussier.
    assert "Points de vigilance" in prompt
    assert "Catalyseurs" in prompt

    # Rappel explicite : aide à la lecture, pas une décision.
    assert "aide à la lecture" in prompt
    assert "pas une décision d'investissement" in prompt or "PAS un conseil en investissement" in prompt

    # Canevas de réponse attendu (sections demandées).
    assert "Résumé" in prompt
    assert "Sources" in prompt
    assert "Date des données" in prompt


def test_build_shortlist_prompt_lists_tokens_in_order():
    tokens = [_snapshot("WLDUSDC"), _snapshot("BTCUSDC"), _snapshot("ETHUSDC")]
    prompt = f.build_shortlist_prompt(tokens, NOW)
    assert prompt.index("WLDUSDC") < prompt.index("BTCUSDC") < prompt.index("ETHUSDC")
