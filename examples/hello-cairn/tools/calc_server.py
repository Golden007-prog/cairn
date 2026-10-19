"""hello-cairn's MCP tool server: two small tools over stdio (ADR-0127 D2).

Glue, not an operator. The `ask-the-docs` pipeline reaches it through the builtin
`mcp/call` operator, so the call is governed and audited like any other tool call; this
script only answers. The MCP client starts it as a subprocess, named in the engine's
environment (cairn/services/mcp/client.py reads `CAIRN_MCP_SERVERS`):

    export CAIRN_MCP_SERVERS='{"hello-calc": {"transport": "stdio", "command": "python",
                               "args": ["examples/hello-cairn/tools/calc_server.py"]}}'

Run it by hand with `python tools/calc_server.py` (it waits for a client on stdin).
"""
from __future__ import annotations

import re

from mcp.server.fastmcp import FastMCP

server = FastMCP("hello-calc")


@server.tool()
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


@server.tool()
def word_count(text: str) -> dict:
    """Count the words and characters in a piece of text."""
    words = re.findall(r"\S+", text or "")
    return {"words": len(words), "chars": len(text or "")}


if __name__ == "__main__":
    server.run()  # stdio transport by default
