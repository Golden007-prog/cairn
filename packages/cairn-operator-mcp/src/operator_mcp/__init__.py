"""obs-operator-mcp — universal MCP dispatcher operator pack.

Distribution identity (``obs-operator-mcp``) is declared in
``pack.toml``. The single operator (``mcp/call``) is surfaced via the
``cairn.operators`` entry-point group registered in this pack's
``pyproject.toml``.

The operator wraps :mod:`cairn.services.mcp` — the substrate-owned
typed-client interface. No transport / connection / auth code lives in
this pack; per locked architecture, providers (this operator) consume
core services (``cairn.services.mcp``), and services own the
cross-cutting concerns (connection pool, schema normalization, replay).
"""
