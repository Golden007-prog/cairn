"""Pydantic schemas for mcp/call (§24.20.10 — operator packs ship Pydantic;
domain packs ship JSON Schema YAML)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CallInput(BaseModel):
    """Inputs for invoking a single MCP tool.

    ``server`` is the logical name from the deploying profile's
    ``[mcp_servers.<name>]`` table; the substrate's MCP client resolves
    it to a concrete transport + URL/command. ``tool`` is the tool's
    name as published by that server. ``args`` is the typed payload the
    tool's own schema expects — opaque to this operator (the substrate's
    typecheck against the cached MCP tool manifest happens in the
    binding compiler at Phase 3+).
    """

    server: str = Field(
        ...,
        description="Logical MCP server name (resolved by profile).",
    )
    tool: str = Field(
        ...,
        description="Tool name on that server (as published by the server's manifest).",
    )
    args: dict[str, Any] = Field(
        default_factory=dict,
        description="Args dict; must conform to the tool's input schema.",
    )


class CallOutput(BaseModel):
    """Result of an MCP tool invocation.

    ``ok`` is the success flag — operators consistently bubble failures
    via this field rather than raising, so callers can branch in the
    state graph without exception plumbing.

    ``result`` is the parsed tool output (dict / list / scalar / None
    depending on the tool). ``services/mcp/client.py:_parse`` handles
    the text/JSON ambiguity at the boundary so the payload here is
    already typed-Python.
    """

    ok: bool
    result: Any = None
    error: str = ""
