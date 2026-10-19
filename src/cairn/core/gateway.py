"""LLM-gateway backends — live spend / routing / health for the console Gateway view.

The console's :class:`GatewaySummary` is sourced from a configured gateway through
a small adapter seam (one :class:`GatewayBackend` protocol, swappable per env /
connection). Two adapters ship first — **LiteLLM proxy** and **OpenRouter** — each
filling the fields its backend actually exposes; what it can't source stays 0/empty.

No gateway configured → :func:`default_gateway_backend` returns ``None`` and the
view gets an EMPTY summary flagged ``X-Cairn-Stub`` — never sample figures. cairn already governs cost
per-run (``obs.cost_scope`` / ``BudgetExceeded``); this is the read-only metrics
layer for the gateway the runs route through.

Adapters take an optional injected ``httpx.AsyncClient`` so tests drive them with
``httpx.MockTransport`` — no live gateway required.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import os
from typing import Optional, Protocol

import httpx
from pydantic import BaseModel


# The adapters' return shapes. They live here, not with the console that serves them, so
# the gateway needs nothing of the console (#2427). No docstrings: a model's docstring
# becomes its OpenAPI description, and the committed spec does not change.
class GatewayRoute(BaseModel):
    tag: str           # job tag: "route" | "synthesize" | "critique" | "agent"
    model: str
    provider: str
    why: str = ""
    calls_24h: int = 0
    cost_usd: float = 0.0


class GatewaySummary(BaseModel):
    spend_today_usd: float = 0.0
    cap_usd: float = 0.0
    calls_24h: int = 0
    providers_healthy: int = 0
    routes: list[GatewayRoute] = []


class GatewayBackend(Protocol):
    async def summary(self) -> GatewaySummary: ...


class LiteLLMGateway:
    """LiteLLM proxy. Per-tag spend → routes (``/spend/tags``), model health
    (``/model/info``). Auth: ``Bearer <master key>``.

    NB: LiteLLM's ``/global/spend/report`` only groups by team|customer|api_key,
    NOT tag — ``/spend/tags`` is the per-tag endpoint. cairn's job tags land here
    when the client sends them as ``x-litellm-tags`` (see ProxyGateway); until a
    call is tagged, LiteLLM buckets by an auto-tag (e.g. User-Agent)."""

    provider = "LiteLLM"
    _MAX_ROUTES = 12  # keep the view readable; totals are summed over ALL tags

    def __init__(self, base_url: str, key: str, *, client: Optional[httpx.AsyncClient] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.key = key
        self._client = client  # injected in tests (MockTransport); else built per call

    def _client_or_new(self) -> httpx.AsyncClient:
        return self._client or httpx.AsyncClient(
            base_url=self.base_url, headers={"Authorization": f"Bearer {self.key}"}, timeout=10.0
        )

    async def summary(self) -> GatewaySummary:
        c = self._client_or_new()
        try:
            routes: list[GatewayRoute] = []
            spend_total = 0.0
            calls_total = 0
            r = await c.get("/spend/tags")
            if r.status_code == 200:
                for row in (r.json() or []):
                    spend = float(row.get("total_spend") or 0.0)
                    calls = int(row.get("log_count") or 0)
                    spend_total += spend
                    calls_total += calls
                    routes.append(
                        GatewayRoute(
                            tag=str(row.get("individual_request_tag") or "—"),
                            model="",
                            provider=self.provider,
                            why="live spend by tag",
                            calls_24h=calls,
                            cost_usd=round(spend, 4),
                        )
                    )
            # Totals span every tag; show only the top spenders in the table.
            routes.sort(key=lambda x: x.cost_usd, reverse=True)
            healthy = 0
            mr = await c.get("/model/info")
            if mr.status_code == 200:
                healthy = len((mr.json() or {}).get("data") or [])
            return GatewaySummary(
                spend_today_usd=round(spend_total, 2),
                cap_usd=0.0,
                calls_24h=calls_total,
                providers_healthy=healthy,
                routes=routes[: self._MAX_ROUTES],
            )
        finally:
            if self._client is None:
                await c.aclose()


class OpenRouterGateway:
    """OpenRouter. Key usage + limit (``/api/v1/key``), available model count
    (``/api/v1/models``). Auth: ``Bearer <OPENROUTER_API_KEY>``. OpenRouter has no
    cairn job-tag routing, so ``routes`` is empty (spend/cap/health only)."""

    provider = "OpenRouter"
    base_url = "https://openrouter.ai/api/v1"

    def __init__(self, key: str, *, client: Optional[httpx.AsyncClient] = None) -> None:
        self.key = key
        self._client = client

    def _client_or_new(self) -> httpx.AsyncClient:
        return self._client or httpx.AsyncClient(
            base_url=self.base_url, headers={"Authorization": f"Bearer {self.key}"}, timeout=10.0
        )

    async def summary(self) -> GatewaySummary:
        c = self._client_or_new()
        try:
            usage = cap = 0.0
            kr = await c.get("/key")
            if kr.status_code == 200:
                d = (kr.json() or {}).get("data") or {}
                usage = float(d.get("usage") or 0.0)
                cap = float(d["limit"]) if d.get("limit") is not None else 0.0
            healthy = 0
            mr = await c.get("/models")
            if mr.status_code == 200:
                healthy = len((mr.json() or {}).get("data") or [])
            return GatewaySummary(
                spend_today_usd=round(usage, 2),
                cap_usd=round(cap, 2),
                calls_24h=0,
                providers_healthy=healthy,
                routes=[],
            )
        finally:
            if self._client is None:
                await c.aclose()


def default_gateway_backend() -> Optional[GatewayBackend]:
    """Pick a gateway from the env, or ``None`` (→ an empty, flagged summary).

    ``CAIRN_GATEWAY=litellm`` needs ``LITELLM_BASE_URL`` + ``LITELLM_MASTER_KEY``.
    ``CAIRN_GATEWAY=openrouter`` needs ``OPENROUTER_API_KEY``. The keys are the same
    SecretStore references a ``litellm`` / ``openrouter`` connection resolves to —
    connection-store-driven selection is the follow-up; env is the v1 selector."""
    kind = getenv("CAIRN_GATEWAY", "").strip().lower()
    if kind == "litellm":
        base = os.environ.get("LITELLM_BASE_URL", "").strip()
        key = os.environ.get("LITELLM_MASTER_KEY", "").strip()
        if base and key:
            return LiteLLMGateway(base, key)
    if kind == "openrouter":
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if key:
            return OpenRouterGateway(key)
    # #692: if cairn already ROUTES through a LiteLLM-compatible proxy
    # (LLM_GATEWAY=proxy — the same config the LLM client uses), reuse it for the
    # console Gateway view. One config lights up BOTH routing and the metrics view
    # — no separate CAIRN_GATEWAY / LITELLM_BASE_URL needed.
    if os.environ.get("LLM_GATEWAY", "").strip().lower() == "proxy":
        base = os.environ.get("LLM_GATEWAY_URL", "").strip()
        key = os.environ.get("LLM_GATEWAY_API_KEY", "").strip()
        if base and key:
            return LiteLLMGateway(base, key)
    return None
