"""Pluggable LLM gateway — routes calls through a proxy or direct to providers.

Supports any OpenAI-compatible proxy: Bifrost, LiteLLM Proxy, Portkey, etc.

Configuration via env vars:
  LLM_GATEWAY=direct     → DirectGateway (no proxy, current behavior)
  LLM_GATEWAY=proxy      → ProxyGateway (route through LLM_GATEWAY_URL)

Examples:
  Bifrost:      LLM_GATEWAY=proxy LLM_GATEWAY_URL=http://localhost:8080
  LiteLLM:      LLM_GATEWAY=proxy LLM_GATEWAY_URL=http://localhost:4000
  Portkey:      LLM_GATEWAY=proxy LLM_GATEWAY_URL=https://api.portkey.ai/v1 LLM_GATEWAY_API_KEY=pk-...
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from functools import lru_cache

logger = logging.getLogger(__name__)


class LLMGateway(ABC):
    """Contract for LLM call routing."""

    @abstractmethod
    def apply(self, params: dict, job_tag: str = "") -> dict:
        """Mutate litellm/ChatOpenAI params dict. Return the modified dict."""
        ...


class DirectGateway(LLMGateway):
    """No proxy — call providers directly (current default behavior)."""

    def apply(self, params: dict, job_tag: str = "") -> dict:
        return params


class ProxyGateway(LLMGateway):
    """Route through an OpenAI-compatible proxy (Bifrost, LiteLLM Proxy, etc.)."""

    def __init__(
        self,
        url: str,
        api_key: str = "",
        job_tag_header: str = "x-job-tag",
        add_provider_prefix: bool = False,
    ):
        self.url = url
        self.api_key = api_key
        self.job_tag_header = job_tag_header
        self.add_provider_prefix = add_provider_prefix

    def apply(self, params: dict, job_tag: str = "") -> dict:
        params["base_url"] = self.url
        if self.api_key:
            params["api_key"] = self.api_key

        # Bifrost requires provider/model format (e.g. "openai/gpt-4o-mini").
        # When enabled, look up the provider from the model registry.
        if self.add_provider_prefix:
            model = params.get("model", "")
            if "/" not in model:
                params["model"] = _prefix_provider(model)

        if job_tag:
            hdrs = params.setdefault("extra_headers", {})
            if self.job_tag_header:
                hdrs[self.job_tag_header] = job_tag
            # LiteLLM buckets spend by the comma-separated `x-litellm-tags`
            # header, so send the job tag there too — then cairn's tag shows in
            # the proxy's /spend/tags (and the console Gateway view, #692).
            hdrs["x-litellm-tags"] = job_tag
        return params


def _prefix_provider(model: str) -> str:
    """Resolve 'gpt-4o-mini' → 'openai/gpt-4o-mini' using the model registry."""
    try:
        from cairn.core.llm.models import _load_registry
        for cfg in _load_registry().values():
            if cfg.name == model:
                return f"{cfg.provider}/{model}"
    except Exception:
        pass
    # Fallback: assume openai for known model prefixes
    if model.startswith(("gpt-", "o1-", "o3-")):
        return f"openai/{model}"
    return model


@lru_cache(maxsize=1)
def get_gateway() -> LLMGateway:
    """Return the active LLM gateway based on settings."""
    from cairn.core.provider_settings import get_settings
    s = get_settings()
    name = (s.llm_gateway or "direct").lower()

    if name == "proxy":
        if not s.llm_gateway_url:
            logger.warning("LLM_GATEWAY=proxy but LLM_GATEWAY_URL is empty — falling back to direct")
            return DirectGateway()
        logger.info("LLM gateway: proxy → %s", s.llm_gateway_url)
        return ProxyGateway(
            url=s.llm_gateway_url,
            api_key=s.llm_gateway_api_key,
            job_tag_header=s.llm_gateway_job_tag_header,
            add_provider_prefix=s.llm_gateway_add_provider_prefix,
        )

    if name != "direct":
        logger.warning("Unknown LLM_GATEWAY=%r — falling back to direct", name)

    return DirectGateway()


def reset_gateway() -> None:
    """Drop the cached gateway (tests, or after gateway env changes)."""
    get_gateway.cache_clear()
