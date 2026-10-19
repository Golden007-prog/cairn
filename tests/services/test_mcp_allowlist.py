"""tool_allow_list_strict enforcement at the MCP client (deployment-scoped)."""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.services.mcp.client import McpClient, McpToolNotAllowed


class _FakeTool:
    def __init__(self, name):
        self.name = name

    async def ainvoke(self, args):
        return {"ok": True, "tool": self.name}


def _client_with_tools(*names):
    c = McpClient(connections={"github": {"transport": "stdio", "command": "x"}})
    c._tools_cache["github"] = {n: _FakeTool(n) for n in names}
    return c


def _spec(strict):
    return SimpleNamespace(tool_allow_list_strict=strict)


def _profile(monkeypatch, *, strict, declared_tools):
    """Patch profile (strict flag) + pack registry (declared tool surface)."""
    monkeypatch.setattr(
        "cairn.core.pack.profile.load_profile_from_env",
        lambda: SimpleNamespace(mcp_servers={"github": _spec(strict)}),
    )
    pack = SimpleNamespace(
        name="p",
        manifest=SimpleNamespace(required_mcp=SimpleNamespace(tools=declared_tools)),
    )
    monkeypatch.setattr("cairn.core.pack.registry.get_packs", lambda: [pack])
    monkeypatch.setattr("cairn.core.pack.ensure_loaded", lambda **kw: None)


@pytest.mark.asyncio
async def test_strict_allows_declared_tool(monkeypatch):
    _profile(monkeypatch, strict=True, declared_tools=["github:list_issues"])
    c = _client_with_tools("list_issues", "delete_repo")
    out = await c.call_tool("github", "list_issues", {})
    assert out["ok"] is True


@pytest.mark.asyncio
async def test_strict_denies_undeclared_tool(monkeypatch):
    _profile(monkeypatch, strict=True, declared_tools=["github:list_issues"])
    c = _client_with_tools("list_issues", "delete_repo")
    with pytest.raises(McpToolNotAllowed) as exc:
        await c.call_tool("github", "delete_repo", {})
    assert "delete_repo" in str(exc.value) and "list_issues" in str(exc.value)


@pytest.mark.asyncio
async def test_non_strict_server_unaffected(monkeypatch):
    _profile(monkeypatch, strict=False, declared_tools=[])
    c = _client_with_tools("delete_repo")
    out = await c.call_tool("github", "delete_repo", {})
    assert out["ok"] is True  # no strict flag → no enforcement


@pytest.mark.asyncio
async def test_no_profile_means_no_enforcement(monkeypatch):
    def _boom():
        raise FileNotFoundError("no profile")
    monkeypatch.setattr("cairn.core.pack.profile.load_profile_from_env", _boom)
    c = _client_with_tools("anything")
    out = await c.call_tool("github", "anything", {})
    assert out["ok"] is True


@pytest.mark.asyncio
async def test_strict_with_empty_declared_surface_denies_all(monkeypatch):
    _profile(monkeypatch, strict=True, declared_tools=[])
    c = _client_with_tools("list_issues")
    with pytest.raises(McpToolNotAllowed):
        await c.call_tool("github", "list_issues", {})
