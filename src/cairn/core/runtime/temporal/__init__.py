"""Temporal runtime — same StateGraph as in-process, durable execution.

The plugin (``temporalio.contrib.langgraph``) reads each node's metadata to
decide whether it runs inline in the workflow body (``execute_in='workflow'``)
or as a separate Activity (``execute_in='activity'``). The :class:`NodeKind`
tags stamped by the compiler (Step 1) drive that translation.

Layout::

  retry.py        # per-NodeKind retry/timeout defaults + per-template override
  temporal/
    __init__.py   # is_available + lazy re-exports
    registry.py   # worker-side compiled-template store (name + sha → graph)
    runtime.py    # TemporalRuntime — submits workflows from the caller side
    workflow.py   # @workflow.defn TemplateWorkflow (imports temporalio)
    activities.py # @activity.defn run_node (imports temporalio)
    worker.py     # bootstrap: load packs → compile all → start Temporal worker

``temporalio`` is an *optional* dep. Importing this package is safe without it;
only :func:`require` raises if missing. The InProcessRuntime path never reaches
into this subpackage at all.
"""

from __future__ import annotations

import importlib.util


def is_available() -> bool:
    """True iff ``temporalio[langgraph]`` is importable."""
    return importlib.util.find_spec("temporalio") is not None and (
        importlib.util.find_spec("temporalio.contrib.langgraph") is not None
    )


def require() -> None:
    """Raise a helpful ImportError if the Temporal SDK + plugin aren't installed."""
    if not is_available():
        raise ImportError(
            "cairn Temporal runtime requires the Temporal Python SDK "
            "with the LangGraph plugin. Install with:\n\n"
            '    pip install "temporalio[langgraph]>=1.27.0"\n'
        )


__all__ = ["is_available", "require"]
