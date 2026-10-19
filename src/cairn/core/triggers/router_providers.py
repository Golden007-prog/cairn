"""Router providers: the ``cairn.routers`` entry-point group (ADR-0120 D4, #2422).

The server mounts the public API (ADR-0120 D2) itself and nothing else. Every other HTTP
surface — today the console's — reaches the app through this group, so a deployment that
installs only the public core serves only D2, and absent providers are never a crash.

A provider is a callable registered under the group::

    [project.entry-points."cairn.routers"]
    datasets = "my_platform.routers:datasets"

that takes a :class:`RouterDeps` and returns an ``APIRouter`` or a list of them (an empty
list when it mounted something on ``deps.app`` itself, e.g. a sub-app or static files).
Feature-flag gating stays with each provider. Providers mount in entry-point name order,
after every public route; a provider marked ``mounts_last = True`` (one that derives from
the fully-routed app, like the ops-MCP) mounts after the rest.

Like the pack and operator loaders, this one fails open: a provider that does not import,
raises, or returns something that is not a router is skipped with a logged warning naming
it. ``cairn doctor`` lists the registered providers.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points
from typing import Any, AsyncContextManager, Callable, Optional

from fastapi import APIRouter, FastAPI

_log = logging.getLogger("cairn.core.triggers.router_providers")

ENTRY_POINT_GROUP = "cairn.routers"


@dataclass
class RouterDeps:
    """What ``build_app`` hands a router provider: the objects the app was built with.

    ``lifespan`` collects async context factories (called with the app) that the server
    enters at startup and unwinds at shutdown — for a provider that mounts a sub-app with
    its own lifespan."""

    app: FastAPI
    runner: Any
    environment: Optional[str]
    dispatcher: Any
    pipeline_bridge: Any
    scheduler: Any = None
    review_store: Any = None
    lifespan: list[Callable[[FastAPI], AsyncContextManager[Any]]] = field(default_factory=list)


@dataclass(frozen=True)
class RouterProvider:
    """One registered provider: its entry-point name and value, and the loaded callable
    (``None`` with ``error`` set when it did not import)."""

    name: str
    value: str
    fn: Optional[Callable[[RouterDeps], Any]] = None
    error: Optional[str] = None


def _entry_points() -> list[EntryPoint]:
    return sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda ep: ep.name)


def discover_router_providers() -> list[RouterProvider]:
    """Every registered provider, loaded, in mount order. Never raises."""
    out: list[RouterProvider] = []
    for ep in _entry_points():
        try:
            fn = ep.load()
        except Exception as e:  # noqa: BLE001 — a broken provider must not take the server down
            out.append(RouterProvider(ep.name, ep.value, error=f"{type(e).__name__}: {e}"))
            continue
        if not callable(fn):
            out.append(RouterProvider(ep.name, ep.value, error=f"resolved to {fn!r}, not a callable"))
            continue
        out.append(RouterProvider(ep.name, ep.value, fn=fn))
    return sorted(out, key=lambda p: bool(getattr(p.fn, "mounts_last", False)))


def load_router_providers() -> list[RouterProvider]:
    """The providers ``build_app`` mounts. Tests stub this to build the public-only app."""
    return discover_router_providers()


def mount_router_providers(app: FastAPI, deps: RouterDeps) -> list[str]:
    """Mount every registered provider on ``app``; return the names that mounted."""
    mounted: list[str] = []
    for p in load_router_providers():
        if p.fn is None:
            _log.warning("router provider %r (%s) not loaded: %s", p.name, p.value, p.error)
            continue
        try:
            result = p.fn(deps)
        except Exception as e:  # noqa: BLE001 — fail open, like the pack/operator loaders
            _log.warning("router provider %r (%s) not mounted: %s: %s",
                         p.name, p.value, type(e).__name__, e)
            continue
        routers = result if isinstance(result, (list, tuple)) else [result]
        if not all(isinstance(r, APIRouter) for r in routers):
            _log.warning("router provider %r (%s) returned %r, not an APIRouter or a list "
                         "of them — skipped", p.name, p.value, result)
            continue
        for r in routers:
            app.include_router(r)
        mounted.append(p.name)
    return mounted


__all__ = [
    "ENTRY_POINT_GROUP", "RouterDeps", "RouterProvider",
    "discover_router_providers", "load_router_providers", "mount_router_providers",
]
