"""#182 — LLM-gateway backends (LiteLLM + OpenRouter) behind the GatewayBackend
seam. Hermetic: adapters are driven with httpx.MockTransport — no live gateway. The
/console/api/gateway endpoint's tests went to the platform with the console (#2427).
"""
from __future__ import annotations

import asyncio

import httpx

from cairn.core.gateway import LiteLLMGateway, OpenRouterGateway, default_gateway_backend


def _litellm_handler(req: httpx.Request) -> httpx.Response:
    # /spend/tags is LiteLLM's per-tag spend endpoint (NOT /global/spend/report,
    # which only groups by team|customer|api_key). Shape verified live (#692).
    if req.url.path == "/spend/tags":
        return httpx.Response(200, json=[
            {"individual_request_tag": "synthesize", "log_count": 266, "total_spend": 2.92},
            {"individual_request_tag": "route", "log_count": 854, "total_spend": 0.31},
        ])
    if req.url.path == "/model/info":
        return httpx.Response(200, json={"data": [{"model_name": "a"}, {"model_name": "b"}, {"model_name": "c"}]})
    return httpx.Response(404)


def test_litellm_gateway_summary():
    client = httpx.AsyncClient(base_url="http://litellm.test", transport=httpx.MockTransport(_litellm_handler))
    g = LiteLLMGateway("http://litellm.test", "sk-master", client=client)
    s = asyncio.run(g.summary())
    assert round(s.spend_today_usd, 2) == 3.23   # summed over all tags
    assert s.providers_healthy == 3
    assert s.calls_24h == 1120                    # summed log_count
    routes = {r.tag: r for r in s.routes}
    assert routes["synthesize"].cost_usd == 2.92
    assert routes["synthesize"].provider == "LiteLLM"
    assert routes["synthesize"].calls_24h == 266
    # sorted by spend desc — the bigger spender comes first
    assert s.routes[0].tag == "synthesize"


def _openrouter_handler(req: httpx.Request) -> httpx.Response:
    if req.url.path.endswith("/key"):
        return httpx.Response(200, json={"data": {"usage": 12.5, "limit": 50.0}})
    if req.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "x"}, {"id": "y"}]})
    return httpx.Response(404)


def test_openrouter_gateway_summary():
    client = httpx.AsyncClient(base_url="https://openrouter.ai/api/v1", transport=httpx.MockTransport(_openrouter_handler))
    g = OpenRouterGateway("sk-or", client=client)
    s = asyncio.run(g.summary())
    assert s.spend_today_usd == 12.5
    assert s.cap_usd == 50.0
    assert s.providers_healthy == 2
    assert s.routes == []  # OpenRouter has no cairn job-tag routing


def test_default_gateway_backend_env_selection(monkeypatch):
    for k in ("OBS_GATEWAY", "OPENROUTER_API_KEY", "LITELLM_BASE_URL", "LITELLM_MASTER_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert default_gateway_backend() is None  # unconfigured

    monkeypatch.setenv("OBS_GATEWAY", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    assert isinstance(default_gateway_backend(), OpenRouterGateway)

    monkeypatch.setenv("OBS_GATEWAY", "litellm")
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm.test")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-master")
    assert isinstance(default_gateway_backend(), LiteLLMGateway)


def test_default_gateway_backend_reuses_llm_gateway_proxy(monkeypatch):
    # #692: the SAME LLM_GATEWAY=proxy routing config lights up the console view —
    # no separate OBS_GATEWAY / LITELLM_BASE_URL needed.
    for k in ("OBS_GATEWAY", "OPENROUTER_API_KEY", "LITELLM_BASE_URL", "LITELLM_MASTER_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_GATEWAY", "proxy")
    monkeypatch.setenv("LLM_GATEWAY_URL", "http://litellm:4000")
    monkeypatch.setenv("LLM_GATEWAY_API_KEY", "sk-master")
    b = default_gateway_backend()
    assert isinstance(b, LiteLLMGateway)
    assert b.base_url == "http://litellm:4000"
