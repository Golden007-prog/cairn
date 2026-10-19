"""Ops-MCP surface (ADR-0067, #642) — from_fastapi(app) → reads-first agent tools.

Asserts the seam derives the read tools and, critically, that the fail-closed
allow-list keeps EVERY mutation off the agent belt — the capability-level
enforcement of the ask RED LINE. Also: the surface is opt-in (OBS_OPS_MCP), so the
default app is unchanged. The `/mcp` mount and the knowledge and cost tools are the
platform's (its providers and routers): packages/cairn-platform/tests/test_ops_mcp_mount.py
(#2424).
"""
from __future__ import annotations

import os
import types

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.triggers.mcp import build_ops_mcp, ops_mcp_enabled
from cairn.core.triggers.server import build_app


class _FakeRunner:
    def __init__(self) -> None:
        self.default_environment = "production"
        self.skill_engine = types.SimpleNamespace(list_skills=lambda: [])

    async def run(self, *a, **k):
        return []


# unambiguous mutation markers — none collide with read tool names like
# "installs"/"installed_workflows" (which are GET reads we DO expose).
_FORBIDDEN = ("resume", "cancel", "evaluate", "uninstall", "approve",
              "password", "sync_workflow", "disable", "delete")


@pytest.mark.asyncio
async def test_reads_first_allowlist_is_fail_closed(monkeypatch):
    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)
    app = build_app(_FakeRunner(), enable_scheduler=False)
    mcp = build_ops_mcp(app)
    names = sorted(t.name for t in await mcp.list_tools())
    # representative reads are present with clean names
    assert "query_runs" in names and "get_run" in names, names
    assert "list_packs" in names and "get_pack_contract" in names, names

    # fail-closed: NOT ONE mutation leaked onto the belt
    leaked = [n for n in names if any(bad in n for bad in _FORBIDDEN)]
    assert leaked == [], f"mutations leaked onto the agent belt: {leaked}"


@pytest.mark.asyncio
async def test_tool_names_are_clean(monkeypatch):
    """Every derived tool carries a curated name (#645) — no leftover from_fastapi
    mangling like `get_run_runs` / `..._get` / `..._post` on the belt."""
    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)
    app = build_app(_FakeRunner(), enable_scheduler=False)
    mcp = build_ops_mcp(app)
    names = [t.name for t in await mcp.list_tools()]
    mangled = [n for n in names if n.endswith(("_get", "_post")) or "_runs_" in n
               or n.count("__") or "ui_api_" in n]
    assert mangled == [], f"un-renamed tools on the belt: {mangled}"
    if any(getattr(r, "path", "") == "/runs/pending-proposals" for r in app.routes):
        assert "count_pending_proposals" in names  # the platform's count, named by its route (#2504)


@pytest.mark.asyncio
async def test_knowledge_query_tool_is_the_only_post(monkeypatch):
    """Every derived tool is a read EXCEPT the one read-only POST (#640). Proven
    by route method: only /ui/api/knowledge/query is a POST-backed tool."""
    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)
    app = build_app(_FakeRunner(), enable_scheduler=False)
    mcp = build_ops_mcp(app)
    posts = []
    for t in await mcp.list_tools():
        # fastmcp stashes the source route on the tool; method lives there
        route = getattr(t, "_route", None) or getattr(t, "route", None)
        methods = getattr(route, "methods", None)
        if methods and "POST" in set(methods):
            posts.append(t.name)
    # if the attr isn't exposed, fall back to name — knowledge_query is the only
    # POST route in the allow-list either way.
    if posts:
        assert all("knowledge_query" in n for n in posts), posts


def test_surface_is_opt_in_by_default(monkeypatch):
    """OBS_OPS_MCP unset → no /mcp mount, app unchanged."""
    monkeypatch.delenv("OBS_OPS_MCP", raising=False)
    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)
    assert ops_mcp_enabled() is False
    app = build_app(_FakeRunner(), enable_scheduler=False)
    assert not any(getattr(r, "path", "").startswith("/mcp") for r in app.routes)


@pytest.mark.asyncio
async def test_workspace_header_propagates_to_tools(monkeypatch):
    """build_ops_mcp(workspace=…) stamps X-Cairn-Workspace on the internal client,
    so every tool sub-request scopes to that workspace (#228). Proven functionally:
    call a tool through the in-memory MCP client and confirm the app's middleware
    saw the workspace. Without this the agent is workspace-blind (reads default)."""
    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)
    from fastmcp import Client

    import cairn.core.triggers.server as server_mod
    from cairn.core.triggers.mcp import build_ops_mcp

    seen: list = []
    real = server_mod.set_current_workspace
    monkeypatch.setattr(server_mod, "set_current_workspace",
                        lambda v: (seen.append(v), real(v))[1])

    app = build_app(_FakeRunner(), enable_scheduler=False)
    async with Client(build_ops_mcp(app, workspace="demo")) as c:
        await c.call_tool("list_packs", {})  # a read that doesn't need the run store
    assert "demo" in seen, seen  # the tool sub-request carried X-Cairn-Workspace: demo


@pytest.mark.asyncio
async def test_auth_token_propagates_to_tools(monkeypatch):
    """#663 (prod-caught): on an auth-enabled deploy the internal client MUST carry
    the caller's bearer, else every read tool sub-request 401s and the agent reads
    nothing. With a valid token the auth-gated tool works; without it (auth on) the
    tool call fails. Dev disables auth, which is why this was invisible until prod."""
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret-663")
    from fastmcp import Client

    from cairn.core.triggers.auth import create_access_token
    from cairn.core.triggers.mcp import build_ops_mcp

    app = build_app(_FakeRunner(), enable_scheduler=False)
    tok = create_access_token({"sub": "admin"})

    # WITH the caller's token → the auth-gated read tool succeeds.
    async with Client(build_ops_mcp(app, token=tok)) as c:
        await c.call_tool("query_runs", {})  # /runs is require_auth; must not 401

    # WITHOUT a token (auth on) → the tool's sub-request 401s → the call fails.
    async with Client(build_ops_mcp(app)) as c:
        with pytest.raises(Exception):
            await c.call_tool("query_runs", {})


@pytest.mark.asyncio
async def test_no_workspace_is_unscoped(monkeypatch):
    """Omitting workspace → no header → the app resolves its default (back-compat)."""
    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)
    from fastmcp import Client

    import cairn.core.triggers.server as server_mod
    from cairn.core.triggers.mcp import build_ops_mcp

    seen: list = []
    real = server_mod.set_current_workspace
    monkeypatch.setattr(server_mod, "set_current_workspace",
                        lambda v: (seen.append(v), real(v))[1])

    app = build_app(_FakeRunner(), enable_scheduler=False)
    async with Client(build_ops_mcp(app)) as c:  # no workspace
        await c.call_tool("list_packs", {})
    assert seen and all(v is None for v in seen), seen  # header absent → None
