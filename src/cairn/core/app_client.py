"""In-process / network client seam for the cairn app (ADR-0067, #647).

One factory so the CLI, ``cairn ask``, and the console all reach the SAME
governed FastAPI app as HTTP clients — the ADR's "everyone is a client of the
app". Two transports behind one call shape:

- **LOCAL (default, single-binary):** httpx over ``ASGITransport(build_app(...))``
  — the request never leaves the process (~0 latency, no server, no open port).
  This is how ``cairn`` works with no server running.
- **NETWORK:** httpx over a ``base_url`` + bearer token — when an endpoint is
  configured (explicit arg or ``CAIRN_REMOTE_ENDPOINT``), talk to a running server.

Reads need no app lifespan, so the local client is lightweight (scheduler off).
Callers own closing: ``async with app_client() as c: ...``.

This is the foundation the ask-consumer + CLI migration build on; it does NOT
change any existing caller yet (``ask/resolve.py`` and ``remote.py`` migrate onto
it in follow-ups).
"""
from __future__ import annotations
from cairn.core.settings import getenv

import os
from typing import Any, Optional

import httpx  # base dep

#: Base URL stamped on the in-process client. Arbitrary — ASGITransport routes by
#: path, not host — but a stable, obviously-fake host keeps logs unambiguous.
_LOCAL_BASE = "http://cairn.local"


def resolve_endpoint(endpoint: Optional[str] = None) -> Optional[str]:
    """The network endpoint to talk to, or ``None`` → use the in-process app.

    Precedence: explicit ``endpoint`` > ``CAIRN_REMOTE_ENDPOINT`` env > ``None``.
    Profile-file resolution stays in the CLI (which passes the result in here),
    so this core module never imports the cli layer. Unlike ``remote.py`` there
    is deliberately NO ``localhost:9090`` fallback — absence of an endpoint means
    "run against myself in-process", the single-binary default.
    """
    ep = endpoint or getenv("CAIRN_REMOTE_ENDPOINT")
    return ep.rstrip("/") if ep else None


def _resolve_token(token: Optional[str]) -> Optional[str]:
    return token or getenv("CAIRN_REMOTE_TOKEN")


_APP_CACHE: Any = None


def set_local_app(app: Any) -> None:
    """Seed the in-process app used by :func:`cairn_client`/:func:`app_client` —
    the engine calls this at startup so server-side consumers (the /ask flow's
    resolve reads, #1196) reach the SAME app instead of building a second one."""
    global _APP_CACHE
    _APP_CACHE = app


def build_local_app() -> Any:
    """The app for the in-process client: the seeded engine app when running
    server-side, else built once per process (the default runner, scheduler off
    — a CLI read shouldn't spin up a scheduler)."""
    global _APP_CACHE
    if _APP_CACHE is None:
        # lazy: building the app pulls the whole server graph — only pay it when local.
        from cairn.core.triggers.main import build_default_runner  # noqa: PLC0415
        from cairn.core.triggers.server import build_app  # noqa: PLC0415

        _APP_CACHE = build_app(build_default_runner(), enable_scheduler=False)
    return _APP_CACHE


def app_client(
    *,
    endpoint: Optional[str] = None,
    token: Optional[str] = None,
    app: Any = None,
    timeout: float = 30.0,
) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` bound to the cairn app — network when an endpoint
    is configured, else in-process over ``ASGITransport``. Same call shape either
    way; the caller just uses paths (``c.get("/packs")``).

    ``app`` overrides the in-process app (tests / an already-built app); ignored
    in network mode.
    """
    ep = resolve_endpoint(endpoint)
    if ep:
        tok = _resolve_token(token)
        headers = {"Authorization": f"Bearer {tok}"} if tok else {}
        return httpx.AsyncClient(base_url=ep, headers=headers, timeout=timeout)
    local = app if app is not None else build_local_app()
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=local), base_url=_LOCAL_BASE, timeout=timeout
    )


def cairn_client(
    *,
    endpoint: Optional[str] = None,
    token: Optional[str] = None,
    app: Any = None,
    timeout: float = 30.0,
) -> Any:
    """The TYPED twin of :func:`app_client` (#1196, ADR-0085 D7): an
    ``AsyncCairnClient`` bound to the app — network when an endpoint is
    configured, else in-process over ``ASGITransport``. cairn-client itself
    never imports cairn; this factory is the one place the in-process app is
    injected (dependency direction: cairn → cairn-client)."""
    from cairn_client import AsyncCairnClient  # noqa: PLC0415 — keep module import light; the client pulls httpx eagerly

    ep = resolve_endpoint(endpoint)
    if ep:
        return AsyncCairnClient(ep, token=_resolve_token(token), timeout=timeout)
    local = app if app is not None else build_local_app()
    return AsyncCairnClient(
        _LOCAL_BASE, timeout=timeout, transport=httpx.ASGITransport(app=local)
    )


__all__ = ["app_client", "cairn_client", "resolve_endpoint", "build_local_app", "set_local_app"]
