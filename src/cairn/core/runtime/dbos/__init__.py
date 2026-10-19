"""DBOS runtime — same StateGraph as in-process, single-node durable execution.

``CAIRN_RUNTIME=dbos`` wraps each run as a DBOS workflow: crash-recovery on
restart, durable HITL via ``DBOS.recv`` / ``DBOS.send``, run-id as the
idempotency key — a library, no separate server, backed by SQLite (dev) or
Postgres (prod) per ADR-0035.

Division of labour (ADR-0035): the **LangGraph checkpointer**
(``SqliteSaver`` / ``PostgresSaver``, ``CAIRN_CHECKPOINTER``) owns resume from the
last super-step; **DBOS** owns liveness (auto-restart PENDING runs on
``launch()``), HITL, and run-id idempotency. The in-graph chokepoints are NOT
wrapped as ``@DBOS.step`` — that would fight the checkpointer's node-skipping on
resume; the mid-node-crash window is covered by operator idempotency instead.

Layout::

  dbos/
    __init__.py   # is_available + require (lazy optional-dep guard)
    runtime.py    # DBOSRuntime — submits/resumes runs from the caller side
    workflow.py   # the run workflow: guarded ainvoke + DBOS.recv resume loop

``dbos`` is an *optional* dep. Importing this package is safe without it; only
:func:`require` raises if missing. The in-process path never reaches here.
"""

from __future__ import annotations

import importlib.util


def is_available() -> bool:
    """True iff the ``dbos`` package is importable."""
    return importlib.util.find_spec("dbos") is not None


def require() -> None:
    """Raise a helpful ImportError if the DBOS library isn't installed."""
    if not is_available():
        raise ImportError(
            "cairn DBOS runtime requires the DBOS Python library. "
            "Install with:\n\n"
            '    pip install "dbos>=2.0"\n'
        )


__all__ = ["is_available", "require"]
