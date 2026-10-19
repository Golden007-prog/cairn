"""Surface a durable-runtime approval pause to the run store (#456, #1847).

The in-process dispatcher marks a gated run ``awaiting_approval`` when it sees
``__interrupt__`` come back from ``ainvoke``. A DURABLE runtime never gives it
that chance: the graph pauses INSIDE the workflow, so the submit call blocks and
the dispatcher's pause branch is never reached. The run then sits at ``running``
— and ``POST /runs/{id}/resume`` refuses it, because that endpoint guards on
``status == "awaiting_approval"``. The gate is real, the checkpoint is durable,
and the run is unapprovable.

Temporal fixed this in #456 with a ``cairn.surface_approval`` activity. DBOS
shipped the same pause shape and never got the fix (#1847), so a DBOS-gated run
hung until its 24h ``recv`` timeout. This is that write, lifted to ONE function
both runtimes call — a chokepoint has to be something others invoke, not a
pattern each copy re-implements slightly differently.

Best-effort by design: a store hiccup must not crash a workflow that is
otherwise fine (the decision channel still works), but it is logged loudly,
because a silent failure here means a run that nobody can approve.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("cairn.core.approval.surface")


async def surface_awaiting_approval(
    run_id: str, approval: Optional[Any], *, store: Optional[Any] = None,
) -> bool:
    """Mark ``run_id`` awaiting approval and record WHAT is being approved.

    ``approval`` is the value passed to ``interrupt()`` — the ApprovalRequest
    dict (operator / risk / sources / reason). It is what
    ``/runs/{id}/pending-approval`` and the console approval card read, so a
    reviewer sees the decision rather than a bare "awaiting_approval".

    ``store`` is the run store to write to (#1865: the DBOS workflow passes the
    server's, since ``default_store()`` builds a fresh in-memory store per call
    when CAIRN_STORAGE is unset); None → ``default_store()``.

    Returns True when the store write landed; False (logged) when it did not.
    """
    if not run_id:
        return False
    try:
        from cairn.core.storage import default_store  # noqa: PLC0415 — cycle: storage → dispatch → runtime

        if store is None:
            store = default_store()
        await store.update(run_id, status="awaiting_approval")
        await store.append_event(
            run_id, "awaiting_approval",
            {"approval": approval} if approval is not None else {},
        )
        return True
    except Exception as e:  # noqa: BLE001 — never crash the workflow over this
        logger.warning(
            "surface_awaiting_approval: store write failed for %s: %s — the run "
            "is paused at a gate but will not appear in the approval queue", run_id, e)
        return False


__all__ = ["surface_awaiting_approval"]
