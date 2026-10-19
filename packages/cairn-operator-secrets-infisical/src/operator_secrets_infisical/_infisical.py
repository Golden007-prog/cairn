"""Shared Infisical helpers for secrets/infisical.

Config (env) + universal-auth login + the authenticated SDK client live here
so ``get`` and ``available_keys`` can't drift on how the project is reached.
The Infisical SDK is imported lazily (it's the pack's one external dep, the
``[infisical]`` extra) so the module imports — and mock mode runs — without
it; a missing SDK raises a clear error only when a real call is made.

Auth is Infisical **universal auth** (machine identity): a ``client_id`` /
``client_secret`` pair minted for a machine identity, never a user token —
credentials come from env, never hard-coded.

Env:
  INFISICAL_CLIENT_ID      (required) — universal-auth machine identity id
  INFISICAL_CLIENT_SECRET  (required) — universal-auth machine identity secret
  INFISICAL_PROJECT_ID     (required) — target project id
  INFISICAL_ENVIRONMENT    (default "dev") — environment slug
  INFISICAL_SECRET_PATH    (default "/") — secret folder path
  INFISICAL_HOST           (default "https://app.infisical.com") — site URL
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

_DEFAULT_HOST = "https://app.infisical.com"
_DEFAULT_ENVIRONMENT = "dev"
_DEFAULT_SECRET_PATH = "/"


class InfisicalConfigError(RuntimeError):
    """A required Infisical env var is missing at call time."""


@dataclass(frozen=True)
class InfisicalConfig:
    """Resolved-at-call-time Infisical connection config (from env)."""

    client_id: str
    client_secret: str
    project_id: str
    environment: str
    secret_path: str
    host: str


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise InfisicalConfigError(f"{name} is not set")
    return val


def load_config() -> InfisicalConfig:
    """Read the Infisical connection config from the environment.

    Raises :class:`InfisicalConfigError` if a required var is missing — a
    misconfigured deployment fails loud at read time, not silently.
    """
    return InfisicalConfig(
        client_id=_require("INFISICAL_CLIENT_ID"),
        client_secret=_require("INFISICAL_CLIENT_SECRET"),
        project_id=_require("INFISICAL_PROJECT_ID"),
        environment=os.environ.get("INFISICAL_ENVIRONMENT", _DEFAULT_ENVIRONMENT),
        secret_path=os.environ.get("INFISICAL_SECRET_PATH", _DEFAULT_SECRET_PATH),
        host=os.environ.get("INFISICAL_HOST", _DEFAULT_HOST),
    )


def authed_client(cfg: InfisicalConfig) -> Any:
    """An Infisical SDK client logged in via universal (machine identity) auth.

    The SDK is imported here (not at module top) so importing the pack without
    the ``[infisical]`` extra never breaks — only a real call needs it.
    """
    try:
        from infisical_sdk import (  # noqa: PLC0415 — optional dependency (infisical extra)
            InfisicalSDKClient,
        )
    except ImportError as exc:  # pragma: no cover — exercised only without the SDK
        raise RuntimeError(
            "the Infisical secrets backend requires the Infisical SDK; install the "
            'extra — `pip install "cairndev[infisical]"`.'
        ) from exc

    client = InfisicalSDKClient(host=cfg.host)
    client.auth.universal_auth.login(
        client_id=cfg.client_id, client_secret=cfg.client_secret
    )
    return client
