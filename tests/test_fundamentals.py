"""Tests de `fundamentals` (Lot 5, §5 CDC) : résolution CoinGecko, parsing JSON défensif,
dégradation gracieuse, gestion de l'environnement.

Aucun appel réseau réel : CoinGecko/DefiLlama sont mockés via `httpx.MockTransport`, et le
client Anthropic est un double de test injecté (jamais le vrai SDK). Aucune clé API n'est
nécessaire pour lancer cette suite.
"""
from __future__ import annotations

import json
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


@pytest.fixture
def anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    return "sk-ant-test-key"


# --------------------------------------------------------------------------- #
# Doubles de test pour le client Anthropic (jamais le vrai SDK)                #
# --------------------------------------------------------------------------- #
class _FakeCountTokensResult:
    def __init__(self, input_tokens: int) -> None:
        self.input_tokens = input_tokens


class _FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeServerToolUseBlock:
    type = "server_tool_use"

    def __init__(self, name: str) -> None:
        self.name = name


class _FakeMessage:
    def __init__(self, content: list, usage: _FakeUsage, stop_reason: str = "end_turn") -> None:
        self.content = content
        self.usage = usage
        self.stop_reason = stop_reason


class _FakeMessagesApi:
    def __init__(
        self,
        response_texts: list[str] | None = None,
        input_tokens: int = 100,
        output_tokens: int = 50,
    ) -> None:
        # Une réponse par appel `create()` (permet de tester la relance sur pause_turn).
        self._response_texts = response_texts if response_texts is not None else ["{}"]
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self.create_calls = 0

    def count_tokens(self, model: str, messages: list) -> _FakeCountTokensResult:
        return _FakeCountTokensResult(input_tokens=self._input_tokens)

    def create(self, model: str, max_tokens: int, tools: list, messages: list) -> _FakeMessage:
        index = min(self.create_calls, len(self._response_texts) - 1)
        text = self._response_texts[index]
        stop_reason = "pause_turn" if self.create_calls + 1 < len(self._response_texts) else "end_turn"
        self.create_calls += 1
        return _FakeMessage(
            content=[_FakeTextBlock(text)],
            usage=_FakeUsage(self._input_tokens, self._output_tokens),
            stop_reason=stop_reason,
        )


class _FakeAnthropicClient:
    def __init__(self, response_texts: list[str] | None = None, **kwargs) -> None:
        self.messages = _FakeMessagesApi(response_texts, **kwargs)


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
# Parsing défensif du JSON renvoyé par le LLM (§5.3 CDC)                       #
# --------------------------------------------------------------------------- #
def test_parse_llm_json_plain_valid():
    raw = '{"resume": "ok", "points_positifs": ["a"], "points_vigilance": [], "catalyseurs": [], "sources": [], "date_donnees": "2026-07-13"}'
    parsed = f.parse_llm_json(raw)
    assert parsed["resume"] == "ok"


def test_parse_llm_json_with_code_fence():
    raw = '```json\n{"resume": "ok"}\n```'
    assert f.parse_llm_json(raw) == {"resume": "ok"}


def test_parse_llm_json_with_bare_fence():
    raw = '```\n{"resume": "ok"}\n```'
    assert f.parse_llm_json(raw) == {"resume": "ok"}


def test_parse_llm_json_with_preamble():
    raw = 'Voici mon analyse :\n{"resume": "ok", "sources": ["CoinDesk"]}'
    assert f.parse_llm_json(raw) == {"resume": "ok", "sources": ["CoinDesk"]}


def test_parse_llm_json_malformed_raises_value_error():
    with pytest.raises((ValueError, json.JSONDecodeError)):
        f.parse_llm_json("ceci n'est pas du JSON du tout")


def test_parse_llm_json_unterminated_object_raises():
    with pytest.raises(ValueError):
        f.parse_llm_json('texte avant { "resume": "ok"')


# --------------------------------------------------------------------------- #
# prepare_run — dégradation gracieuse (CoinGecko en échec, token non résolu)   #
# --------------------------------------------------------------------------- #
def test_prepare_run_degrades_gracefully_on_coingecko_failure(cfg, coingecko_key, anthropic_key):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/search":
            return httpx.Response(500, text="internal error")
        if request.url.path == "/protocols":
            return httpx.Response(200, json=[])
        raise AssertionError(f"URL inattendue : {request.url}")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    anthropic_client = _FakeAnthropicClient(input_tokens=42)

    plan = f.prepare_run(["ABCUSDC"], cfg, http_client, anthropic_client)

    assert len(plan.tokens) == 1
    token_plan = plan.tokens[0]
    assert token_plan.resolved is None
    assert token_plan.market_data is None
    assert any("CoinGecko" in err for err in token_plan.errors)
    # Le run continue malgré l'échec : un prompt est bien construit et compté.
    assert token_plan.estimated_input_tokens == 42
    assert plan.estimated_input_tokens == 42


def test_prepare_run_unresolved_token_is_logged_not_guessed(cfg, coingecko_key, anthropic_key):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/search":
            return httpx.Response(200, json={"coins": []})
        if request.url.path == "/protocols":
            return httpx.Response(200, json=[])
        raise AssertionError(f"URL inattendue : {request.url}")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    anthropic_client = _FakeAnthropicClient()

    plan = f.prepare_run(["GHOSTUSDC"], cfg, http_client, anthropic_client)
    token_plan = plan.tokens[0]
    assert token_plan.resolved is None
    assert "non résolu" in token_plan.errors[0]


def test_prepare_run_computes_cost_estimate_and_budget(cfg, coingecko_key, anthropic_key):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/search":
            return httpx.Response(200, json={"coins": []})
        raise AssertionError(f"URL inattendue : {request.url}")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    anthropic_client = _FakeAnthropicClient(input_tokens=1000)

    cfg_small_budget = cfg.model_copy(
        update={
            "fundamentals": cfg.fundamentals.model_copy(
                update={"max_tokens_per_run": 100, "sources": cfg.fundamentals.sources.model_copy(
                    update={"defillama": cfg.fundamentals.sources.defillama.model_copy(update={"enabled": False})}
                )}
            )
        }
    )

    plan = f.prepare_run(["ABCUSDC"], cfg_small_budget, http_client, anthropic_client)
    assert plan.estimated_input_tokens == 1000
    assert plan.over_budget is True  # 1000 entrée + sortie pire cas >> 100


# --------------------------------------------------------------------------- #
# execute_run — parsing défensif intégré, dégradation gracieuse                #
# --------------------------------------------------------------------------- #
def _minimal_plan(prompt: str = "prompt de test") -> f.FundamentalsRunPlan:
    token_plan = f.TokenPlan(
        symbol="ABCUSDC",
        resolved=None,
        market_data=None,
        tvl_usd=None,
        prompt=prompt,
        estimated_input_tokens=10,
        errors=[],
        fetched_at=NOW,
    )
    return f.FundamentalsRunPlan(
        tokens=[token_plan], estimated_input_tokens=10, estimated_max_output_tokens=100,
        estimated_max_web_searches=5, estimated_cost_usd=0.001, over_budget=False,
    )


def test_execute_run_valid_synthesis(cfg):
    valid_json = json.dumps(
        {
            "resume": "Réseau L1 en croissance.",
            "points_positifs": ["adoption en hausse"],
            "points_vigilance": ["forte volatilité"],
            "catalyseurs": ["mise à jour prévue"],
            "sources": ["CoinDesk"],
            "date_donnees": "2026-07-13",
        }
    )
    anthropic_client = _FakeAnthropicClient(response_texts=[valid_json], input_tokens=10, output_tokens=20)
    report = f.execute_run(_minimal_plan(), cfg, anthropic_client, now_func=_now)

    assert report.usage.total_input_tokens == 10
    assert report.usage.total_output_tokens == 20
    token = report.tokens[0]
    assert token.synthesis is not None
    assert token.synthesis.resume == "Réseau L1 en croissance."
    assert token.errors == []


def test_execute_run_malformed_json_marks_synthesis_unavailable_without_crash(cfg):
    anthropic_client = _FakeAnthropicClient(response_texts=["texte incohérent, pas de JSON"], input_tokens=10, output_tokens=5)
    report = f.execute_run(_minimal_plan(), cfg, anthropic_client, now_func=_now)

    token = report.tokens[0]
    assert token.synthesis is None
    assert any("non parsable" in err for err in token.errors)
    # Le run se termine normalement (pas d'exception propagée) et compte quand même l'usage.
    assert report.usage.total_input_tokens == 10


def test_execute_run_handles_pause_turn_continuation(cfg):
    """Deux tours (pause_turn puis end_turn) : l'usage est cumulé sur les deux appels."""
    responses = ["texte intermédiaire", json.dumps({"resume": "ok"})]
    anthropic_client = _FakeAnthropicClient(response_texts=responses, input_tokens=10, output_tokens=5)
    report = f.execute_run(_minimal_plan(), cfg, anthropic_client, now_func=_now)

    assert anthropic_client.messages.create_calls == 2
    assert report.usage.total_input_tokens == 20  # 10 + 10
    assert report.usage.total_output_tokens == 10  # 5 + 5
    assert report.tokens[0].synthesis is not None


def test_count_web_search_calls_counts_only_web_search_server_tool_use():
    blocks = [
        _FakeServerToolUseBlock("web_search"),
        _FakeServerToolUseBlock("code_execution"),
        _FakeTextBlock("hello"),
        _FakeServerToolUseBlock("web_search"),
    ]
    assert f._count_web_search_calls(blocks) == 2


def test_prepare_run_cost_estimate_includes_web_search_worst_case(cfg, coingecko_key, anthropic_key):
    """Le coût des recherches web (tarif vérifié : $10/1000, plat) est chiffré dans l'estimation,
    jamais laissé de côté — pire cas = web_search_max_uses par token analysé."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/search":
            return httpx.Response(200, json={"coins": []})
        if request.url.path == "/protocols":
            return httpx.Response(200, json=[])
        raise AssertionError(f"URL inattendue : {request.url}")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    anthropic_client = _FakeAnthropicClient(input_tokens=100)

    plan = f.prepare_run(["ABCUSDC"], cfg, http_client, anthropic_client)
    fcfg = cfg.fundamentals
    pricing = fcfg.pricing_usd_per_million_tokens

    assert plan.estimated_max_web_searches == fcfg.web_search_max_uses  # 1 seul token dans ce run
    expected_cost = (
        100 / 1_000_000 * pricing.input
        + fcfg.max_tokens_per_call / 1_000_000 * pricing.output
        + plan.estimated_max_web_searches / 1000 * pricing.web_search_per_1000
    )
    assert plan.estimated_cost_usd == pytest.approx(expected_cost)
    # Le coût web_search n'est pas nul : il pèse bien dans le total (jamais "non chiffré").
    assert plan.estimated_max_web_searches > 0


def test_execute_run_prices_web_search_calls_in_real_cost(cfg):
    """Le coût réel (post-exécution) inclut les recherches web effectivement comptées."""
    class _MessagesApiWithWebSearch:
        def create(self, model: str, max_tokens: int, tools: list, messages: list) -> _FakeMessage:
            content = [
                _FakeServerToolUseBlock("web_search"),
                _FakeServerToolUseBlock("web_search"),
                _FakeTextBlock(json.dumps({"resume": "ok"})),
            ]
            return _FakeMessage(content=content, usage=_FakeUsage(10, 20), stop_reason="end_turn")

    class _ClientWithWebSearch:
        messages = _MessagesApiWithWebSearch()

    report = f.execute_run(_minimal_plan(), cfg, _ClientWithWebSearch(), now_func=_now)

    assert report.usage.web_search_calls == 2
    pricing = cfg.fundamentals.pricing_usd_per_million_tokens
    expected_cost = (
        10 / 1_000_000 * pricing.input
        + 20 / 1_000_000 * pricing.output
        + 2 / 1000 * pricing.web_search_per_1000
    )
    assert report.usage.estimated_cost_usd == pytest.approx(expected_cost)


def test_execute_run_anthropic_error_does_not_crash_the_run(cfg):
    class _RaisingMessagesApi:
        def create(self, **kwargs):
            raise RuntimeError("panne réseau simulée")

    class _RaisingClient:
        messages = _RaisingMessagesApi()

    report = f.execute_run(_minimal_plan(), cfg, _RaisingClient(), now_func=_now)
    token = report.tokens[0]
    assert token.synthesis is None
    assert any("Appel de synthèse Claude en échec" in err for err in token.errors)
