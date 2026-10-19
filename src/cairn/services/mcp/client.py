"""Code-facing MCP client — call a named tool on an external MCP server.

This is the piece that lets *our code* (notifiers, approval channels,
post-mortem publishers) call official MCP servers — Slack, Notion,
Confluence, GitHub, etc. — deterministically, as opposed to the agent_loop
which calls MCP tools by reasoning. It complements ``src/mcp/hub.py`` (which
exposes OUR observability tools to the LLM via Bifrost).

Design choices
--------------
* **Secrets stay in env.** Server connection specs (command, url, tokens)
  are resolved from environment variables, never hard-coded.
* **Graceful when unconfigured.** ``is_configured(name)`` lets callers fall
  back (webhook, local file, no-op) instead of crashing a demo that has no
  Slack token.
* **Transport-agnostic.** Built on ``langchain_mcp_adapters``'
  ``MultiServerMCPClient`` so a server can be a local stdio subprocess
  (``npx @modelcontextprotocol/server-slack``) or a remote HTTP/SSE endpoint
  (e.g. routed through Bifrost's MCP gateway).

Configuration
-------------
Two ways to declare servers (env):

1. Inline JSON (full control), ``CAIRN_MCP_SERVERS``::

       {"slack": {"transport": "stdio",
                   "command": "npx",
                   "args": ["-y", "@modelcontextprotocol/server-slack"],
                   "env": {"SLACK_BOT_TOKEN": "...", "SLACK_TEAM_ID": "..."}},
        "notion": {"transport": "streamable_http",
                    "url": "http://localhost:9100/mcp"}}

2. Per-integration convenience vars (stdio or http)::

       CAIRN_MCP_SLACK_COMMAND="npx"   CAIRN_MCP_SLACK_ARGS='["-y","@modelcontextprotocol/server-slack"]'
       CAIRN_MCP_NOTION_URL="http://localhost:9100/mcp"

The inline JSON wins when both are present for a server.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import json
import logging
import os
from typing import Any

logger = logging.getLogger("cairn.mcp_client")

_KNOWN = ("slack", "notion", "confluence", "github")


class McpNotConfigured(RuntimeError):
    """Raised when a tool call targets an MCP server that isn't configured."""


class McpToolNotAllowed(RuntimeError):
    """Raised when ``tool_allow_list_strict`` is on for a server and the tool
    isn't in any installed pack's declared ``[requires.mcp].tools`` surface."""


# ─── Connection resolution ───────────────────────────────────────────


def _inline_servers() -> dict[str, dict]:
    raw = getenv("CAIRN_MCP_SERVERS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("CAIRN_MCP_SERVERS is not valid JSON: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def _convenience_server(name: str) -> dict | None:
    """Build a connection spec from CAIRN_MCP_<NAME>_* env vars, or None."""
    prefix = f"CAIRN_MCP_{name.upper()}_"
    url = getenv(prefix + "URL")
    command = getenv(prefix + "COMMAND")
    if url:
        transport = getenv(prefix + "TRANSPORT", "streamable_http")
        spec: dict[str, Any] = {"transport": transport, "url": url}
    elif command:
        args_raw = getenv(prefix + "ARGS", "[]")
        try:
            args = json.loads(args_raw)
        except json.JSONDecodeError:
            args = args_raw.split()
        spec = {"transport": "stdio", "command": command, "args": args}
        env_raw = getenv(prefix + "ENV")
        if env_raw:
            try:
                spec["env"] = json.loads(env_raw)
            except json.JSONDecodeError:
                logger.warning("%sENV is not valid JSON; ignoring", prefix)
    else:
        return None
    return spec


def server_connections() -> dict[str, dict]:
    """Resolve all configured MCP server connection specs from env."""
    servers = dict(_inline_servers())
    for name in _KNOWN:
        if name in servers:
            continue  # inline JSON wins
        spec = _convenience_server(name)
        if spec is not None:
            servers[name] = spec
    return servers


def is_configured(server: str) -> bool:
    return server in server_connections()


# ─── Result parsing (mirrors src/mcp/hub.py:_parse) ──────────────────


def _parse(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    return json.loads(item.get("text", ""))
                except json.JSONDecodeError:
                    return item.get("text", "")
    return raw


# ─── The client ──────────────────────────────────────────────────────


class McpClient:
    """Lazy, transport-agnostic client for calling external MCP server tools."""

    def __init__(self, connections: dict[str, dict] | None = None) -> None:
        self._connections = connections if connections is not None else server_connections()
        self._client = None  # MultiServerMCPClient, built lazily
        self._tools_cache: dict[str, dict[str, Any]] = {}

    def is_configured(self, server: str) -> bool:
        return server in self._connections

    def _ensure_client(self):
        if self._client is None:
            if not self._connections:
                raise McpNotConfigured("no MCP servers configured")
            from langchain_mcp_adapters.client import MultiServerMCPClient
            self._client = MultiServerMCPClient(self._connections)
        return self._client

    async def _tools_for(self, server: str) -> dict[str, Any]:
        if server not in self._connections:
            raise McpNotConfigured(
                f"MCP server '{server}' is not configured "
                f"(configured: {sorted(self._connections)})"
            )
        if server not in self._tools_cache:
            client = self._ensure_client()
            tools = await client.get_tools(server_name=server)
            self._tools_cache[server] = {t.name: t for t in tools}
        return self._tools_cache[server]

    async def list_tools(self, server: str) -> list[str]:
        return list((await self._tools_for(server)).keys())

    @staticmethod
    def _allowed_tools(server: str) -> set[str] | None:
        """The deployment-scoped tool allowlist for ``server``, or ``None``
        when enforcement is off.

        Enforcement is ON when the active profile declares the server with
        ``tool_allow_list_strict = true`` (deployment decision, identity-free).
        The allowed set is the union of every installed pack's declared
        ``[requires.mcp].tools`` entries for this server (``"<server>:<tool>"``)
        — packs declare the surface they need; strict mode pins calls to it.
        """
        try:
            from cairn.core.pack.profile import load_profile_from_env
            spec = load_profile_from_env().mcp_servers.get(server)
        except Exception:  # noqa: BLE001 — no/broken profile = enforcement off
            return None
        if spec is None or not spec.tool_allow_list_strict:
            return None

        allowed: set[str] = set()
        try:
            from cairn.core.pack import ensure_loaded
            from cairn.core.pack.registry import get_packs
            ensure_loaded()
            prefix = f"{server}:"
            for pack in get_packs():
                req = getattr(getattr(pack, "manifest", None), "required_mcp", None)
                for entry in getattr(req, "tools", None) or []:
                    if entry.startswith(prefix):
                        allowed.add(entry[len(prefix):])
        except Exception:  # noqa: BLE001 — strict mode with unreadable packs → deny-all
            logger.exception("allowlist resolution failed for %r — denying all", server)
        return allowed

    async def call_tool(self, server: str, tool: str, args: dict[str, Any]) -> Any:
        """Invoke ``tool`` on ``server`` with ``args``. Returns parsed result.

        Raises McpNotConfigured if the server isn't configured, KeyError if
        the tool isn't offered by that server, McpToolNotAllowed if the
        deployment enabled ``tool_allow_list_strict`` for this server and no
        installed pack declared the tool.
        """
        allowed = self._allowed_tools(server)
        if allowed is not None and tool not in allowed:
            raise McpToolNotAllowed(
                f"tool '{tool}' on MCP server '{server}' is not in the "
                f"deployment allowlist (strict mode; declared: {sorted(allowed)})"
            )
        by_name = await self._tools_for(server)
        t = by_name.get(tool)
        if t is None:
            raise KeyError(
                f"MCP server '{server}' does not offer tool '{tool}' "
                f"(has: {sorted(by_name)})"
            )
        raw = await t.ainvoke(args)
        return _parse(raw)


# ─── Module singleton ────────────────────────────────────────────────

_default_client: McpClient | None = None


def get_mcp_client() -> McpClient:
    global _default_client
    if _default_client is None:
        _default_client = McpClient()
    return _default_client


# ─── Per-service convenience wrappers ────────────────────────────────


async def slack_post(text: str, channel: str | None = None) -> dict | None:
    """Post a Slack message via the configured Slack MCP server.

    Returns the tool result, or ``None`` if no Slack MCP server is configured
    (so callers can fall back to a webhook or no-op).

    Env knobs (defaults target our mock server; real servers differ):
      CAIRN_MCP_SLACK_TOOL          default "slack_post_message"
      CAIRN_MCP_SLACK_CHANNEL_ARG   default "channel" (official server: "channel_id")
      SLACK_CHANNEL / SLACK_CHANNEL_ID  the destination channel
    """
    client = get_mcp_client()
    if not client.is_configured("slack"):
        return None
    from cairn.core import settings as _settings
    tool = _settings.mcp_slack_tool()
    channel = channel or _settings.slack_channel() or "#incidents"
    channel_arg = _settings.mcp_slack_channel_arg()
    return await client.call_tool("slack", tool, {channel_arg: channel, "text": text})
