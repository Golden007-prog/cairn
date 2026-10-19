"""Resolve a backend SELECTION through the one config plane (#1093, ADR-0083 D3/R3).

``profile_backends.effective_backends`` has always been *the* resolver — profile
record → environment variable → schema default (ADR-0052 §1) — and it already
declares every selector key (``dataflow_engine``, ``ml_engine``, ``runtime``,
``bus``, targets, …). But consumers read the env vars directly, so **a profile
that selected a backend was silently ignored**: with
``[profile.backends] dataflow_engine = "metaflow"`` and no env var,
``resolve_engine`` still returned the builtin engine (P9/P11).

This module is the accessor consumers call instead of ``os.environ``. It loads
the ambient profile once per call (cheap, cached upstream), asks
``effective_backends``, and never raises — a malformed or missing profile
degrades to env-then-default so backend selection cannot break a dispatch.

**Scope — selectors only.** Credential/DSN env reads (``CAIRN_STORAGE=postgres://…``,
``QDRANT_URL``, ``PGVECTOR_DSN``, …) are deliberately NOT routed here: they are
the injection seam of ADR-0052 §3, where
:func:`cairn.core.backend_connections.apply_backend_connection` resolves a bound
connection INTO those env names for the synchronous factories to read. Routing
them through ``effective_backends`` would lose the DSN — it returns the backend
*kind* (``"postgres"``), not the URL. Per-store DB paths (``CAIRN_RUN_DB`` &c.,
problem P10) are likewise out of scope; they belong to the storage-substrate work
(R4), not to selection.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import logging
import os
from typing import Any, Optional

from cairn.core.profile_backends import BACKENDS, effective_backends

logger = logging.getLogger("cairn.core.backend_selection")

_BY_KEY = {f.key: f for f in BACKENDS}


def _ambient_backends() -> dict[str, Any]:
    """``[profile.backends]`` from the ambient profile (``CAIRN_PROFILE``), or ``{}``.

    Never raises: a missing/malformed profile means "the profile says nothing",
    which is exactly the pre-#1093 behavior (env → default).
    """
    from cairn.core.pack.profile import load_profile_from_env  # noqa: PLC0415 - cycle: core.profile → pack

    prof = load_profile_from_env()
    return dict(getattr(prof, "backends", None) or {})


def _resolved(key: str) -> dict[str, Any]:
    try:
        pb = _ambient_backends()
    except Exception as e:  # noqa: BLE001 — selection must survive a bad profile
        logger.debug("no profile backends for %r (%s) — falling back to env/default", key, e)
        pb = {}
    return effective_backends(pb)


def selected(key: str, *, default: str = "") -> str:
    """The effective value of a select/text backend field, as a string.

    ``default`` is returned only for a key the schema doesn't declare (a typo);
    declared keys always resolve through profile → env → schema default.
    """
    if key not in _BY_KEY:
        return default
    return str(_resolved(key).get(key, default) or default)


def selected_bool(key: str, *, default: bool = False) -> bool:
    """The effective value of a toggle backend field."""
    if key not in _BY_KEY:
        return default
    return bool(_resolved(key).get(key, default))


def provenance(key: str) -> str:
    """Which plane decided this field: ``"profile"`` | ``"env"`` | ``"default"``.

    For doctor/console diagnostics — P9's "which config plane wins is per-call-site
    folklore" is only fixed once the answer is *inspectable*. ``"unknown"`` for an
    undeclared key.
    """
    f = _BY_KEY.get(key)
    if f is None:
        return "unknown"
    try:
        pb = _ambient_backends()
    except Exception:  # noqa: BLE001
        pb = {}
    pv = pb.get(key)
    if pv is not None and (f.control == "toggle" or str(pv).strip() != ""):
        return "profile"
    if f.env and str(getenv(f.env, "")).strip():
        return "env"
    return "default"


def effective_report() -> list[dict[str, Any]]:
    """Every backend field with its effective value + provenance + env var name."""
    return [
        {
            "key": f.key,
            "value": _resolved(f.key).get(f.key),
            "provenance": provenance(f.key),
            "env": f.env or "",
        }
        for f in BACKENDS
    ]


def selection_env(keys: Optional[list[str]] = None) -> dict[str, str]:
    """Selector values as the env names consumers/workers already read.

    The bridge for code that cannot import core (an operator pack running on a
    box, a subprocess): resolve control-side, ship the result as env. Only
    non-empty string values are included.
    """
    out: dict[str, str] = {}
    for f in BACKENDS:
        if keys is not None and f.key not in keys:
            continue
        if not f.env or f.control == "toggle":
            continue
        v = selected(f.key)
        if v:
            out[f.env] = v
    return out


__all__ = [
    "selected",
    "selected_bool",
    "provenance",
    "effective_report",
    "selection_env",
]
