"""Runtime layer — how a compiled template's graph actually executes.

The compiler stays runtime-agnostic. It builds a langgraph ``StateGraph`` and
tags each node with a :class:`NodeKind` via :func:`tag`. The runtime decides
what to do with those tags.

  - :class:`InProcessRuntime` (today's default) ignores tags and runs the graph
    via langgraph Pregel in this process.
  - ``TemporalRuntime`` (planned, ``cairn.core.runtime.temporal``) reads the
    tags and routes ``LLM``/``TOOL``/``IO`` nodes to Temporal Activities while
    keeping ``PURE`` nodes inline in the workflow body. The graph definition is
    the same; only the executor differs.

Use :func:`select_runtime` to pick one at startup. The default returns
:class:`InProcessRuntime` so nothing today changes behavior.
"""

from __future__ import annotations

from cairn.core.runtime.base import (
    InvokeFn,
    NodeKind,
    Runtime,
    get_kind,
    tag,
)
from cairn.core.runtime.inprocess import InProcessRuntime


def select_runtime(name: str | None = None) -> Runtime:
    """Return a :class:`Runtime` by name.

    ``name`` is typically ``settings.runtime`` (``"inprocess"`` | ``"dbos"`` |
    ``"temporal"``). Unknown values fall back to in-process — keeps the test
    gate green while durable runtimes are being built.
    """
    if name == "temporal":
        # Import lazily so the Temporal SDK is an optional dep.
        from cairn.core.runtime.temporal.runtime import TemporalRuntime  # noqa: PLC0415

        return TemporalRuntime()
    if name == "dbos":
        # Import lazily so DBOS is an optional dep (ADR-0035).
        from cairn.core.runtime.dbos.runtime import DBOSRuntime  # noqa: PLC0415

        return DBOSRuntime()
    return InProcessRuntime()


__all__ = [
    "InProcessRuntime",
    "InvokeFn",
    "NodeKind",
    "Runtime",
    "get_kind",
    "select_runtime",
    "tag",
]
