"""Model registry — resolve logical roles to actual model configs.

Agents and the router reference roles ("fast", "strong", "router")
instead of provider-specific model names. Switching providers is a
single edit in config/models.yaml — no agent code changes.

Cost tracking reads from here so prices are always in sync with
the actual model being called.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from cairn.core.provider_settings import get_settings

logger = logging.getLogger(__name__)

# Wheel-safe: models.yaml ships next to this loader inside the package.
# The legacy layout walked up to a repo-root `config/` dir; that broke under
# `pip install` (config/ wasn't packaged). Resolved relative to __file__ now.
_CONFIG_PATH = Path(__file__).resolve().parent / "models.yaml"


class ModelConfig(BaseModel):
    role: str
    provider: str
    name: str
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    max_tokens: int = 2048
    temperature: float = 0.3
    source: str = "yaml"  # yaml | env | override — where provider/name came from (#352)


# ── Model catalog (#362) ─────────────────────────────────────────────────────
# Static seed, refreshed manually — public per-1k-token prices as of 2026-07
# (approximate; provider list prices drift). Feeds the console's model picker
# and the server-side cost auto-fill when a role is rebound to a known model.
MODEL_CATALOG: dict[str, list[dict[str, Any]]] = {
    "openai": [
        {"name": "gpt-4o", "cost_per_1k_input": 0.0025, "cost_per_1k_output": 0.01},
        {"name": "gpt-4o-mini", "cost_per_1k_input": 0.00015, "cost_per_1k_output": 0.0006},
        {"name": "gpt-4.1", "cost_per_1k_input": 0.002, "cost_per_1k_output": 0.008},
        {"name": "gpt-4.1-mini", "cost_per_1k_input": 0.0004, "cost_per_1k_output": 0.0016},
        {"name": "gpt-4.1-nano", "cost_per_1k_input": 0.0001, "cost_per_1k_output": 0.0004},
        {"name": "o3", "cost_per_1k_input": 0.002, "cost_per_1k_output": 0.008},
        {"name": "o4-mini", "cost_per_1k_input": 0.0011, "cost_per_1k_output": 0.0044},
    ],
    "anthropic": [
        {"name": "claude-opus-4-8", "cost_per_1k_input": 0.005, "cost_per_1k_output": 0.025},
        {"name": "claude-opus-4-7", "cost_per_1k_input": 0.005, "cost_per_1k_output": 0.025},
        {"name": "claude-opus-4-6", "cost_per_1k_input": 0.005, "cost_per_1k_output": 0.025},
        {"name": "claude-sonnet-5", "cost_per_1k_input": 0.003, "cost_per_1k_output": 0.015},
        {"name": "claude-sonnet-4-6", "cost_per_1k_input": 0.003, "cost_per_1k_output": 0.015},
        {"name": "claude-haiku-4-5-20251001", "cost_per_1k_input": 0.001, "cost_per_1k_output": 0.005},
    ],
    "gemini": [
        {"name": "gemini-2.5-pro", "cost_per_1k_input": 0.00125, "cost_per_1k_output": 0.01},
        {"name": "gemini-2.5-flash", "cost_per_1k_input": 0.0003, "cost_per_1k_output": 0.0025},
        {"name": "gemini-2.5-flash-lite", "cost_per_1k_input": 0.0001, "cost_per_1k_output": 0.0004},
        {"name": "gemini-2.0-flash", "cost_per_1k_input": 0.0001, "cost_per_1k_output": 0.0004},
        {"name": "gemini-2.0-flash-lite", "cost_per_1k_input": 0.000075, "cost_per_1k_output": 0.0003},
    ],
}


def catalog_for(provider: str) -> list[dict[str, Any]]:
    """The known-model list for one provider. Raises KeyError for a provider
    the catalog doesn't seed (gateway/custom providers have no catalog)."""
    return MODEL_CATALOG[provider]


# ── Persisted role overrides (#352) ──────────────────────────────────────────
# UI-authored reassignments live OUTSIDE the wheel (models.yaml ships inside
# it) so they survive upgrades. Precedence: override > MODEL_<ROLE>_NAME env
# > models.yaml; `source` on each role keeps shadowing visible.

def _overrides_path() -> Path:
    return Path(getenv("CAIRN_MODEL_ROLES", "") or Path.home() / ".cairn" / "model_roles.json")


def _load_overrides() -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(_overrides_path().read_text())
        return raw if isinstance(raw, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 — a corrupt overrides file must not take LLM calls down
        logger.warning("model_roles overrides unreadable at %s — ignoring", _overrides_path())
        return {}


@lru_cache(maxsize=1)
def _load_registry() -> dict[str, ModelConfig]:
    with open(_CONFIG_PATH) as f:
        raw = yaml.safe_load(f)
    overrides = _load_overrides()

    registry: dict[str, ModelConfig] = {}
    for role, cfg in raw.get("models", {}).items():
        source = "yaml"
        env_name = f"MODEL_{role.upper()}_NAME"
        if env_name in os.environ:
            cfg["name"] = os.environ[env_name]
            source = "env"

        ov = overrides.get(role)
        if ov and ov.get("name"):
            cfg["name"] = ov["name"]
            if ov.get("provider"):
                cfg["provider"] = ov["provider"]
            # override costs travel with the override — the yaml costs describe
            # the yaml model, not whatever the role was rebound to (#362)
            for key in ("cost_per_1k_input", "cost_per_1k_output"):
                if ov.get(key) is not None:
                    cfg[key] = ov[key]
            source = "override"

        registry[role] = ModelConfig(role=role, source=source, **cfg)
    return registry


def get_model(role: str) -> ModelConfig:
    """Resolve a role to its model config. Falls back to 'fast' for unknown roles."""
    registry = _load_registry()
    if role in registry:
        return registry[role]
    if role in _reverse_lookup():
        return _reverse_lookup()[role]
    logger.warning("Unknown model role %r — falling back to 'fast'", role)
    return registry.get("fast", next(iter(registry.values())))


@lru_cache(maxsize=1)
def _reverse_lookup() -> dict[str, ModelConfig]:
    """Also allow lookup by actual model name for backwards compat."""
    registry = _load_registry()
    return {cfg.name: cfg for cfg in registry.values()}


def resolve_model_name(role_or_name: str) -> str:
    """Given a role or model name, return the actual model name."""
    registry = _load_registry()
    if role_or_name in registry:
        return registry[role_or_name].name
    return role_or_name


def get_cost(model_name: str) -> tuple[float, float]:
    """Return (cost_per_1k_input, cost_per_1k_output) for a model name or role."""
    registry = _load_registry()
    for cfg in registry.values():
        if cfg.name == model_name or cfg.role == model_name:
            return cfg.cost_per_1k_input, cfg.cost_per_1k_output
    return 0.0, 0.0


# ── Role reassignment + provider connectivity (#352) ────────────────────────

def list_roles() -> list[ModelConfig]:
    """Every role in registry order — the gateway page's role table."""
    return list(_load_registry().values())


def _invalidate() -> None:
    _load_registry.cache_clear()
    _reverse_lookup.cache_clear()


def set_role_override(
    role: str,
    provider: str,
    name: str,
    cost_per_1k_input: float | None = None,
    cost_per_1k_output: float | None = None,
) -> ModelConfig:
    """Reassign a role (persisted). Raises KeyError for a role models.yaml
    doesn't declare — roles are the contract agents reference; the UI edits
    their bindings, it doesn't invent new ones.

    Costs: an omitted cost auto-fills from MODEL_CATALOG when provider/name is
    a known catalog model (#362); explicit values win (custom/self-hosted
    models). When neither yields a value the role keeps its yaml costs."""
    if role not in yaml.safe_load(_CONFIG_PATH.read_text()).get("models", {}):
        raise KeyError(role)
    if cost_per_1k_input is None or cost_per_1k_output is None:
        entry = next((m for m in MODEL_CATALOG.get(provider, []) if m["name"] == name), None)
        if entry is not None:
            if cost_per_1k_input is None:
                cost_per_1k_input = entry["cost_per_1k_input"]
            if cost_per_1k_output is None:
                cost_per_1k_output = entry["cost_per_1k_output"]
    path = _overrides_path()
    overrides = _load_overrides()
    ov: dict[str, Any] = {"provider": provider, "name": name}
    if cost_per_1k_input is not None:
        ov["cost_per_1k_input"] = float(cost_per_1k_input)
    if cost_per_1k_output is not None:
        ov["cost_per_1k_output"] = float(cost_per_1k_output)
    overrides[role] = ov
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(overrides, indent=2))
    _invalidate()
    return _load_registry()[role]


def clear_role_override(role: str) -> ModelConfig:
    """Drop a persisted override — the role falls back to env/yaml."""
    overrides = _load_overrides()
    if role in overrides:
        del overrides[role]
        _overrides_path().write_text(json.dumps(overrides, indent=2))
        _invalidate()
    registry = _load_registry()
    if role not in registry:
        raise KeyError(role)
    return registry[role]


def connected_llm_providers() -> dict[str, bool]:
    """Which LLM providers this deployment can actually call — key present in
    Settings/.env/process env, or a gateway proxy configured (a gateway fronts
    any model, so it counts as its own provider). Role reassignment (#352) is
    constrained to the True entries; doctor warns on roles bound to False."""
    settings = get_settings()
    return {
        "openai": bool(settings.openai_api_key or os.environ.get("OPENAI_API_KEY")),
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "gemini": bool(settings.gemini_api_key or os.environ.get("GEMINI_API_KEY")),
        "gateway": bool(settings.llm_gateway != "direct" and settings.llm_gateway_url),
    }
