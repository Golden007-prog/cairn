"""OAuth provider catalog — endpoints + scopes loaded from a file (#116).

Scopes/endpoints live in ``providers.yaml`` next to this module (override the
path with ``CAIRN_OAUTH_CATALOG``) so deployers tune scopes without code changes.
Consumed by :class:`~cairn.core.oauth.broker.AuthlibBroker`.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

_CATALOG_FILE = Path(__file__).resolve().parent / "providers.yaml"


@dataclass(frozen=True)
class OAuthProviderSpec:
    """One SaaS OAuth provider's endpoints + default scopes (from the file)."""

    provider: str
    authorize_url: str
    token_url: str
    scopes: list[str] = field(default_factory=list)
    userinfo_url: Optional[str] = None
    refresh: bool = False
    pkce: bool = False
    extra_authorize_params: dict[str, str] = field(default_factory=dict)
    description: str = ""


_CACHE: Optional[dict[str, OAuthProviderSpec]] = None


def _catalog_path() -> Path:
    override = getenv("CAIRN_OAUTH_CATALOG", "").strip()
    return Path(override) if override else _CATALOG_FILE


def load_oauth_catalog(*, refresh: bool = False) -> dict[str, OAuthProviderSpec]:
    """All provider specs keyed by provider name. Cached; ``refresh=True`` reloads
    (also re-reads ``CAIRN_OAUTH_CATALOG``)."""
    global _CACHE
    if _CACHE is not None and not refresh:
        return _CACHE
    raw = yaml.safe_load(_catalog_path().read_text(encoding="utf-8")) or {}
    out: dict[str, OAuthProviderSpec] = {}
    for name, entry in raw.items():
        entry = entry or {}
        out[str(name)] = OAuthProviderSpec(
            provider=str(name),
            authorize_url=entry["authorize_url"],
            token_url=entry["token_url"],
            scopes=[str(s) for s in (entry.get("scopes") or [])],
            userinfo_url=entry.get("userinfo_url"),
            refresh=bool(entry.get("refresh", False)),
            pkce=bool(entry.get("pkce", False)),
            extra_authorize_params={k: str(v) for k, v in (entry.get("extra_authorize_params") or {}).items()},
            description=str(entry.get("description", "")),
        )
    _CACHE = out
    return out


def get_oauth_provider(name: str) -> Optional[OAuthProviderSpec]:
    return load_oauth_catalog().get(name.strip().lower())


def list_oauth_providers() -> list[OAuthProviderSpec]:
    return list(load_oauth_catalog().values())


def reset_oauth_catalog() -> None:
    """Drop the cache (tests, or after editing the catalog file)."""
    global _CACHE
    _CACHE = None


__all__ = [
    "OAuthProviderSpec",
    "get_oauth_provider",
    "list_oauth_providers",
    "load_oauth_catalog",
    "reset_oauth_catalog",
]
