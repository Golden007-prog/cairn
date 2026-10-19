"""TemplateWorkflow — the durable counterpart to ``InProcessRuntime.invoke``.

One workflow type handles every template. The submitter passes
``(template_name, template_sha, initial_state)``; the workflow looks up the
compiled graph via the LangGraph plugin (which holds the same registry the
worker boot populated) and runs it.

The plugin walks the graph using each node's ``execute_in`` metadata —
``"workflow"`` nodes run inline (deterministic, no Activity round-trip),
``"activity"`` nodes are dispatched via the generic
:func:`cairn.core.runtime.temporal.activities.run_node` activity with the
per-node retry policy + timeout that ``worker.adapt_graph_for_temporal``
stamped at boot.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from cairn.core.runtime.temporal import require

logger = logging.getLogger("cairn.runtime.temporal.workflow")


require()
from temporalio import workflow  # noqa: E402


# Lazy import: temporalio.contrib.langgraph appears only when the plugin
# extras are installed. We don't need the symbol at decoration time — the
# workflow body uses ``graph(name).compile().ainvoke(...)`` and the plugin
# resolves "name" against its registered graphs.
@workflow.defn(name="cairn.TemplateWorkflow")
class TemplateWorkflow:
    """Durable execution of one compiled template.

    Inputs and outputs are intentionally untyped dicts; the workflow doesn't
    know or care about the IncidentMachineState shape — that's the graph's
    concern, not the orchestrator's.
    """

    def __init__(self) -> None:
        # HITL decision delivered by the `approve` signal (ADR-0034).
        self._decision: dict[str, Any] | None = None

    @workflow.signal
    def approve(self, decision: dict[str, Any]) -> None:
        """Deliver a human approval/rejection to a paused gate (ADR-0034).

        ``decision`` is ApprovalRecord-shaped (``{"approved": bool, ...}``) —
        the value the paused ``interrupt()`` returns on resume. Sent by
        ``TemporalRuntime.resume`` (and thus by ``POST /runs/{id}/resume`` →
        ``RunDispatcher.resume_run``, an internal issue) keyed on the
        workflow id.
        """
        self._decision = decision

    @workflow.run
    async def run(
        self,
        template_name: str,
        template_sha: str,  # noqa: ARG002 — surfaced to activities, not used inline
        initial_state: dict[str, Any],
    ) -> dict[str, Any]:
        from langgraph.checkpoint.memory import InMemorySaver  # noqa: PLC0415
        from temporalio.contrib.langgraph import graph  # noqa: PLC0415

        # interrupt() / Command(resume=…) require a LangGraph checkpointer, else
        # pregel raises "Cannot use Command(resume=...) without checkpointer" on
        # resume (#48). Temporal owns durability (event history + deterministic
        # replay), so the documented choice is an in-memory saver — it only
        # satisfies pregel's resume mechanics and is replay-safe because Temporal
        # re-runs the workflow deterministically.
        # See docs.temporal.io/develop/python/integrations/langgraph.
        compiled = graph(template_name).compile(checkpointer=InMemorySaver())
        # Deterministic thread id (the workflow id) ties the initial run and the
        # post-approval resume to the same checkpoint thread.
        cfg = {"configurable": {"thread_id": workflow.info().workflow_id}}
        # Inject run_id into state so nodes key on the run — the durable remote-job
        # record id (run_id:node), the SSH workdir, and compute_target resolution all
        # need it. The in-process dispatcher injects {"run_id": inv.id}; the Temporal
        # path must do the same (workflow_id IS the run_id). #536 e2e fix.
        if isinstance(initial_state, dict) and not initial_state.get("run_id"):
            initial_state = {**initial_state, "run_id": workflow.info().workflow_id}
        result = await compiled.ainvoke(initial_state, cfg)
        # HITL (ADR-0034): if the graph paused at an approval interrupt, durably
        # wait for an `approve` signal and resume — the Temporal counterpart of
        # the in-process checkpointer pause/resume. With no interrupt this loop
        # is skipped, so non-gated workflows behave exactly as before.
        while isinstance(result, dict) and "__interrupt__" in result:
            # #456: SURFACE the pending approval before blocking. Under Temporal
            # execute_workflow blocks here, so the dispatcher never emits
            # awaiting_approval — this activity does (status + ApprovalRequest),
            # so the console shows the gate and POST /runs/{id}/resume can send
            # the `approve` signal we wait on below. Without it the run hangs
            # 'running' forever (issue #456).
            from cairn.core.interrupts import KIND_REMOTE_JOB, interrupt_kind  # noqa: PLC0415

            intr = result["__interrupt__"]
            items = list(intr) if isinstance(intr, (list, tuple)) else [intr]
            raw = getattr(items[0], "value", None) if items else None
            payload = raw
            if hasattr(payload, "model_dump"):
                payload = payload.model_dump(mode="json")
            elif payload is not None and not isinstance(payload, dict):
                payload = {"value": str(payload)}
            # ADR-0065: a remote-job pause is RUNNING — surface it as such, NOT as an
            # approval gate. The resume path (the `approve` signal → Command(resume))
            # is identical; only the surfacing activity differs.
            surface = (
                "cairn.surface_remote_job"
                if interrupt_kind(raw) == KIND_REMOTE_JOB
                else "cairn.surface_approval"
            )
            await workflow.execute_activity(
                surface,
                payload,
                start_to_close_timeout=timedelta(seconds=30),
            )
            await workflow.wait_condition(lambda: self._decision is not None)
            decision, self._decision = self._decision, None
            from langgraph.types import Command  # noqa: PLC0415

            result = await compiled.ainvoke(Command(resume=decision), cfg)
        return dict(result)


WORKFLOWS = [TemplateWorkflow]
