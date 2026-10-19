"""#1775 — a terminal event must be self-describing.

Found dogfooding the builder loop: a task leg's run completed in ~6ms with its
``done`` event logged, but the pipeline's step stayed ``running`` forever. Two
defects conspired:

1. ``_on_run_terminal`` read ``msg.data`` — the bus :class:`Message` carries
   ``payload``, so the #1532 message-borne pipeline id was DEAD CODE and
   correlation depended entirely on the fragile process-local map.
2. The bridge's own emits (task/flow/cancel legs) never passed
   ``pipeline_id=`` to ``dispatcher._emit`` even though it supports it — so
   the payload had no pipeline id to read anyway.

These tests drive ``_on_run_terminal`` with the REAL bus Message shape and an
EMPTY ``_run_to_pipeline`` map (the lost-map / replica case): with the id in
the payload the step must advance; and the task-leg emit path must actually
stamp that id.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.bus import Message
from cairn.core.dispatch.dispatcher import RunDispatcher
from cairn.core.dispatch.pipeline_bridge import PipelineBridge
from cairn.core.engine.runner import Runner
from cairn.core.engine.schema import TriggerEvent
from cairn.core.storage import InMemoryRunStore
from cairn.core.engine.bundle_pipeline import PipelinePlan, PipelinePlanStep
from cairn.core.storage.pipeline_store import InMemoryPipelineStore, PipelineRecord, StepState
from cairn.core.storage.protocols import PIPELINE_ID_KEY, Run


def _harness():
    dispatcher = RunDispatcher(Runner(), store=InMemoryRunStore())
    bridge = PipelineBridge(dispatcher, store=InMemoryPipelineStore())
    return dispatcher, bridge


async def _seed(dispatcher, bridge, *, step="estimate"):
    """One pipeline whose `estimate` step is running, its run already done."""
    run = Run(
        id="run-terminal-test-1",
        trigger=TriggerEvent(source="manual", payload={}),
        status="done",
        template_ref="task:cost/tokens",
        step_id=step,
        workspace_id="default",
        result_payloads=[{"output": {"usd": 0.01}}],
    )
    await dispatcher.store.create(run)
    rec = PipelineRecord(
        pipeline_id="pl_terminal_test",
        bundle_ref="pipeline:workspace/x",
        pipeline_name="x",
        status="running",
        workspace_id="default",
        plan=PipelinePlan(name="x", steps=[
            PipelinePlanStep(id=step, ref="task:cost/tokens", depends_on=[]),
        ]),
        step_state={step: StepState(status="running", run_id=run.id)},
    )
    await bridge.store.put(rec)
    return run, rec


@pytest.mark.asyncio
async def test_payload_pipeline_id_advances_without_the_map():
    """The lost-map / replica case: the map is EMPTY, the payload carries the
    id (as #1532 intended) — the step must still advance."""
    dispatcher, bridge = _harness()
    run, rec = await _seed(dispatcher, bridge)
    assert bridge._run_to_pipeline == {}  # the whole point

    msg = Message(
        topic="run.done",
        payload={"leg": "task:cost/tokens", "step_id": "estimate", "error": "",
                 PIPELINE_ID_KEY: rec.pipeline_id},
        correlation_id=run.id,
    )
    await bridge._on_run_terminal(msg)

    cur = await bridge.store.get(rec.pipeline_id)
    assert cur.step_state["estimate"].status == "done", (
        "run.done with the pipeline id IN THE PAYLOAD did not advance the step "
        "— the #1532 fallback is dead again")


@pytest.mark.asyncio
async def test_task_leg_emit_stamps_pipeline_id():
    """The publisher half: the task-leg terminal must carry the pipeline id so
    the event is self-describing (no reliance on process-local state)."""
    dispatcher, bridge = _harness()

    seen: list[Message] = []
    from cairn.core import bus as busmod
    real = busmod.get_bus()

    class SpyBus:
        async def publish(self, topic, payload, *, publisher=None, correlation_id=None):
            seen.append(Message(topic=topic, payload=dict(payload), correlation_id=correlation_id))
            return await real.publish(topic, payload, publisher=publisher, correlation_id=correlation_id)

    busmod._BUS = SpyBus()  # process-global seam; restored below
    try:
        rec = PipelineRecord(
            pipeline_id="pl_emit_test", bundle_ref="pipeline:workspace/y",
            pipeline_name="y", status="running", workspace_id="default",
            step_state={},
        )
        await bridge.store.put(rec)

        class _Step:
            id = "estimate"
            ref = "task:definitely/not-an-op"
            environment = ""
            timeout_s = None

        run = await bridge._submit_task_leg(rec, _Step(), {"x": 1})
        # let the background leg run to its terminal (unknown op → failed)
        import asyncio
        for _ in range(50):
            await asyncio.sleep(0.02)
            terminals = [m for m in seen if m.topic in ("run.done", "run.failed")]
            if terminals:
                break
        assert terminals, "task leg never emitted a terminal"
        assert terminals[0].payload.get(PIPELINE_ID_KEY) == "pl_emit_test", (
            "task-leg terminal does not carry the pipeline id — the event is "
            "not self-describing (#1775)")
        assert terminals[0].correlation_id == run.id
    finally:
        busmod._BUS = real


@pytest.mark.asyncio
async def test_a_raising_handler_FAILS_the_pipeline_instead_of_stalling():
    """#1825 — the bus contract is at-most-once: a handler that raises is logged
    and DROPPED (§24.10.6). For this handler that turned a crash into a pipeline
    stuck `running` forever with its leg already done, which took a class-level
    spy inside a live engine to diagnose. An exception here is a pipeline
    OUTCOME now: the pipeline fails, carrying the reason.
    """
    dispatcher, bridge = _harness()
    run, rec = await _seed(dispatcher, bridge)

    async def boom(*a, **k):
        raise RuntimeError("predicate exploded")

    bridge._handle_terminal = boom
    msg = Message(topic="run.done",
                  payload={"step_id": "estimate", PIPELINE_ID_KEY: rec.pipeline_id},
                  correlation_id=run.id)
    await bridge._on_run_terminal(msg)          # must NOT raise into the bus

    cur = await bridge.store.get(rec.pipeline_id)
    assert cur.status == "failed", "a raising handler must fail the pipeline, not stall it"
    # the reason travels on the record's trigger (where _fail_pipeline puts it)
    assert "predicate exploded" in str((cur.trigger or {}).get("failure_reason", "")), cur.trigger
