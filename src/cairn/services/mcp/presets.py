"""MCP connector presets — known-good specs for official servers.

Ships reference connector descriptors (``cairn/connectors/*.toml``:
github, slack, notion) so a deployment doesn't reverse-engineer the package
name, transport, and required env for each official MCP server. A preset is
*reference + convenience*, not a new config layer: ``preset_connection(name)``
returns a connection dict in the exact shape the MCP client consumes
(``server_connections()``), with the documented env vars resolved from the
environment. Missing env is surfaced (not silently empty) so doctor / startup
can flag it.

Presets compose with the rest of item 5/7: a pack pins the tools it needs via
``[requires.mcp].tools``; a profile sets ``tool_allow_list_strict`` to deny the
rest (enforced in the MCP client).
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

_CONNECTORS_DIR = Path(__file__).resolve().parents[2] / "connectors"


def list_presets() -> list[str]:
    """Names of the shipped connector presets (sorted)."""
    if not _CONNECTORS_DIR.is_dir():
        return []
    return sorted(p.stem for p in _CONNECTORS_DIR.glob("*.toml"))


def load_preset(name: str) -> dict[str, Any]:
    """The raw parsed connector descriptor (connector/server/env/scopes/tools)."""
    path = _CONNECTORS_DIR / f"{name}.toml"
    if not path.is_file():
        raise KeyError(f"unknown connector preset {name!r}; have: {list_presets()}")
    return tomllib.loads(path.read_text())


def required_env(name: str) -> list[str]:
    """Env var names the preset declares as required (value starts 'required')."""
    env = load_preset(name).get("env", {})
    return [k for k, v in env.items() if str(v).lower().startswith("required")]


def missing_env(name: str) -> list[str]:
    """Required env vars that are currently unset/empty."""
    return [k for k in required_env(name) if not os.environ.get(k)]


def preset_connection(name: str) -> dict[str, Any]:
    """A connection dict for the MCP client (same shape as ``server_connections``).

    Resolves the preset's declared env vars from the current environment into the
    server's ``env`` mapping. Raises ``KeyError`` for an unknown preset; callers
    may check :func:`missing_env` first to fail loud on absent credentials.
    """
    spec = load_preset(name)
    server = dict(spec.get("server", {}))
    command = server.get("command") or []
    conn: dict[str, Any] = {"transport": server.get("transport", "stdio")}
    if command:
        conn["command"] = command[0]
        conn["args"] = list(command[1:])
    if server.get("url"):
        conn["url"] = server["url"]
    declared = list(spec.get("env", {}).keys())
    conn["env"] = {k: os.environ[k] for k in declared if k in os.environ}
    return conn
