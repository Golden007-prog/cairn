"""The ``cairn.routers`` loader (ADR-0120 D4, #2422): providers mount after the public API,
fail open with a warning naming the provider, and the repo's registrations resolve."""
from __future__ import annotations

import importlib
import logging
import tomllib
from importlib.metadata import EntryPoint
from pathlib import Path

from fastapi import APIRouter, FastAPI

from cairn.core.triggers import router_providers as rp

REPO = Path(__file__).resolve().parents[2]


def _router(path: str) -> APIRouter:
    r = APIRouter()
    r.add_api_route(path, lambda: None)
    return r


def _deps(app: FastAPI) -> rp.RouterDeps:
    return rp.RouterDeps(app=app, runner=None, environment=None, dispatcher=None, pipeline_bridge=None)


def test_a_broken_provider_is_skipped_with_a_warning_naming_it(monkeypatch, caplog):
    def boom(deps):
        raise RuntimeError("no store")

    monkeypatch.setattr(rp, "load_router_providers", lambda: [
        rp.RouterProvider("missing", "x.y:z", error="ModuleNotFoundError: x"),
        rp.RouterProvider("boom", "a:boom", fn=boom),
        rp.RouterProvider("odd", "a:odd", fn=lambda deps: "not a router"),
        rp.RouterProvider("good", "a:good", fn=lambda deps: [_router("/good")]),
    ])
    app = FastAPI()
    with caplog.at_level(logging.WARNING, logger=rp._log.name):
        assert rp.mount_router_providers(app, _deps(app)) == ["good"]
    assert [r.path for r in app.routes if r.path == "/good"] == ["/good"]
    for name in ("missing", "boom", "odd"):
        assert any(repr(name) in m for m in caplog.messages), (name, caplog.messages)


def test_providers_mount_in_name_order_and_mounts_last_goes_last(monkeypatch):
    def late(deps):
        return []
    late.mounts_last = True
    loaded = {"a:late": late, "a:b": lambda d: [], "a:c": lambda d: []}
    eps = [EntryPoint(n, v, rp.ENTRY_POINT_GROUP) for n, v in (("c", "a:c"), ("a", "a:late"), ("b", "a:b"))]
    monkeypatch.setattr(rp, "_entry_points", lambda: sorted(eps, key=lambda e: e.name))
    monkeypatch.setattr(EntryPoint, "load", lambda self: loaded[self.value])
    assert [p.name for p in rp.discover_router_providers()] == ["b", "c", "a"]


def test_the_root_registers_no_router_provider():
    """The root pyproject declares no ``cairn.routers`` provider (#2427): everything the public
    server serves is D2, mounted by ``build_app``; every provider is the platform's."""
    eps = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["entry-points"]
    assert "cairn.routers" not in eps, sorted(eps.get("cairn.routers", {}))


def test_every_platform_provider_resolves():
    """The same for ``packages/cairn-platform`` (#2421), where the platform is installed."""
    import pytest

    pytest.importorskip("cairn_platform")
    eps = tomllib.loads((REPO / "packages/cairn-platform/pyproject.toml").read_text())["project"]["entry-points"]["cairn.routers"]
    root = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["entry-points"].get("cairn.routers", {})
    assert eps and not set(eps) & set(root), "a provider name is registered twice"
    for name, value in eps.items():
        mod, _, attr = value.partition(":")
        assert callable(getattr(importlib.import_module(mod), attr)), name
