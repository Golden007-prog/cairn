"""TemporalRuntime — the caller-side :class:`Runtime` impl.

Submits a workflow to a Temporal cluster instead of running the graph in-
process. Today's compiler builds the same StateGraph either way; only the
``invoke`` path differs.

Shape::

    rt = TemporalRuntime(address="localhost:7233", namespace="default",
                         task_queue="cairn")
    state = await rt.invoke(compiled_graph, initial_state,
                            run_id="...", template_name="incident-rca",
                            template_sha="abc123")

The InProcessRuntime ignores ``template_name`` / ``template_sha`` (they're
forwarded as kwargs); the Temporal runtime requires both — without them it
has nothing to submit, since the worker side keys its registry on those.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Optional

from cairn.core import settings
from cairn.core.runtime.temporal import require

logger = logging.getLogger("cairn.runtime.temporal.runtime")


class TemporalRuntime:
    """:class:`Runtime` implementation backed by a Temporal cluster."""

    def __init__(
        self,
        address: Optional[str] = None,
        namespace: Optional[str] = None,
        task_queue: Optional[str] = None,
    ) -> None:
        require()
        self.address = address or settings.temporal_address()
        self.namespace = namespace or settings.temporal_namespace()
        self.task_queue = task_queue or settings.temporal_task_queue()
        self._client: Any = None

    async def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        from temporalio.client import Client  # noqa: PLC0415

        logger.info(
            "connecting to temporal address=%s namespace=%s",
            self.address,
            self.namespace,
        )
        self._client = await Client.connect(self.address, namespace=self.namespace)
        return self._client

    async def invoke(
        self,
        compiled_graph: Any,  # noqa: ARG002 — graph rebuilt on worker side
        initial_state: dict[str, Any],
        *,
        run_id: Optional[str] = None,
        template_name: Optional[str] = None,
        template_sha: Optional[str] = None,
    ) -> dict[str, Any]:
        if template_name is None or template_sha is None:
            raise ValueError(
                "TemporalRuntime.invoke requires template_name and template_sha — "
                "the worker keys its registry on these. The caller (the runner) "
                "should resolve them via TemplateRegistry.sha_for(path) before "
                "calling invoke."
            )
        client = await self._get_client()
        workflow_id = run_id or f"cairn-{template_name}-{template_sha}"

        result = await client.execute_workflow(
            "cairn.TemplateWorkflow",
            args=[template_name, template_sha, dict(initial_state)],
            id=workflow_id,
            task_queue=self.task_queue,
            # #963 — the LangGraph graph runs via `compiled.ainvoke()` INSIDE the
            # workflow body (workflow.py), so a slow deterministic node (e.g. a
            # dataset operator scanning 50k+ rows) blocks the workflow TASK. The
            # default workflow-task timeout is 10s → such nodes fail with
            # "Workflow task exceeded 10 seconds". Raise to Temporal's max (120s)
            # as a mitigation. The real fix is running heavy dataset ops as
            # activities / on the dedicated batch worker (#962).
            task_timeout=timedelta(seconds=120),
        )
        return dict(result) if isinstance(result, dict) else {"value": result}

    async def resume(
        self,
        *,
        run_id: Optional[str] = None,
        decision: Any,
        template_name: Optional[str] = None,
        template_sha: Optional[str] = None,
    ) -> None:
        """Resume a workflow paused at an approval gate (ADR-0034).

        Sends the ``approve`` signal (carrying the ApprovalRecord) to the
        workflow whose gate is waiting — the Temporal counterpart of
        ``InProcessRuntime.resume``.

        **Delivery is verified, not assumed** (#1558). Live on 2026-08-22 the
        signal call returned cleanly, the API answered ``resumed: true``, a
        ``resumed`` event entered the audit log — and the workflow history
        showed NO ``WorkflowExecutionSignaled`` had ever been appended. An
        approval that reports success while doing nothing is the worst failure
        shape a governance product can have, so after signaling we fetch the
        workflow history and require the signal to actually be there; if it
        isn't, this raises and the API returns 502 with the evidence instead
        of lying. Approvals are rare and histories small — the extra fetch is
        cheap where it matters.
        """
        client = await self._get_client()
        workflow_id = run_id or f"cairn-{template_name}-{template_sha}"
        logger.info(
            "resume: signaling workflow=%s via address=%s namespace=%s",
            workflow_id, self.address, self.namespace,
        )
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal("approve", decision)

        import asyncio  # noqa: PLC0415

        from temporalio.api.enums.v1 import EventType  # noqa: PLC0415

        # History visibility can lag the signal ack by a beat (observed on the
        # SDK test server: a signal that demonstrably resumed the workflow read
        # as 0 events when fetched immediately), so poll briefly before calling
        # it lost. A workflow that COMPLETED meanwhile also proves delivery.
        signaled, completed = 0, False
        last_err: Exception | None = None
        for attempt in range(4):
            if attempt:
                await asyncio.sleep(1.5)
            try:
                history = await handle.fetch_history()
            except Exception as exc:  # noqa: BLE001 — keep polling; raise with context below
                last_err = exc
                continue
            for ev in history.events:
                if ev.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED:
                    signaled += 1
                elif ev.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED:
                    completed = True
            if signaled or completed:
                break
        if not signaled and not completed:
            if last_err is not None:
                raise RuntimeError(
                    f"approval signal sent to workflow {workflow_id!r} but history "
                    f"could not be verified (address={self.address} "
                    f"namespace={self.namespace}): {last_err}"
                ) from last_err
            raise RuntimeError(
                f"approval signal to workflow {workflow_id!r} was NOT delivered — "
                f"no WorkflowExecutionSignaled in history after "
                f"{attempt + 1} checks (address={self.address} "
                f"namespace={self.namespace}). The run is still paused; nothing "
                f"was approved. (#1558)"
            )
        logger.info(
            "resume: workflow=%s delivery verified (signals=%d completed=%s)",
            workflow_id, signaled, completed,
        )

    async def terminate(self, run_id: str, *, reason: str) -> bool:
        """Terminate ``run_id``'s workflow (#917).

        The expire/reaper path flipped the RUN to ``expired`` but left the
        workflow parked OPEN on its approval ``wait_condition`` forever —
        zombie workflows accumulated one per expired approval (5 found live in
        the #888 E2E). Termination (not cancel) is deliberate: the run is
        already terminal in the store, so there is nothing for cleanup handlers
        to decide. Returns True iff a workflow was terminated; a missing /
        already-closed workflow is False, never an error.
        """
        try:
            client = await self._get_client()
            await client.get_workflow_handle(run_id).terminate(reason=reason)
            return True
        except Exception as exc:  # noqa: BLE001 — absent/closed workflow is a no-op
            logger.debug("terminate(%s): %s", run_id, exc)
            return False

    async def workflow_is_running(self, workflow_id: str) -> bool:
        """Is this workflow (a pipeline id for the durable owner) currently
        RUNNING? False for terminal, not-found or unreachable — a caller that
        defers to a live owner must never defer to an absent one (#1429)."""
        from temporalio.client import WorkflowExecutionStatus  # noqa: PLC0415

        try:
            client = await self._get_client()
            desc = await client.get_workflow_handle(workflow_id).describe()
        except Exception as exc:  # noqa: BLE001 — unknown ⇒ not alive
            logger.debug("workflow_is_running(%s): describe failed: %s", workflow_id, exc)
            return False
        return desc.status == WorkflowExecutionStatus.RUNNING

    async def workflow_terminal_status(self, run_id: str) -> Optional[str]:
        """Return the cairn terminal status for ``run_id``'s workflow, or None.

        #976 — the reconciler asks this for every run cairn still thinks is
        active. Returns ``done``/``failed``/``cancelled`` when Temporal reports
        the workflow is terminal, or ``None`` when it is still RUNNING / paused /
        CONTINUED_AS_NEW / not found (the run is left untouched in those cases).
        The workflow id IS the run id (see :meth:`invoke`).
        """
        from temporalio.client import WorkflowExecutionStatus  # noqa: PLC0415

        terminal = {
            WorkflowExecutionStatus.COMPLETED: "done",
            WorkflowExecutionStatus.FAILED: "failed",
            WorkflowExecutionStatus.TIMED_OUT: "failed",
            WorkflowExecutionStatus.CANCELED: "cancelled",
            WorkflowExecutionStatus.TERMINATED: "cancelled",
        }
        try:
            client = await self._get_client()
            desc = await client.get_workflow_handle(run_id).describe()
        except Exception as exc:  # noqa: BLE001 — a missing/unreachable workflow is "unknown", not terminal
            logger.debug("workflow_terminal_status(%s): describe failed: %s", run_id, exc)
            return None
        return terminal.get(desc.status)
