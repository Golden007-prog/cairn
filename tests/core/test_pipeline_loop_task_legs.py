"""#1303 — loops over TASK legs, found by the #1300 harness.

Two defects, both pinned here: (1) the bridge's iteration-append carried a
stale pre-#1278 ref stamping, so a task-leg body ran iteration 1 (plan-time
stamp) and died on iteration 2+ dispatched as a template; (2) the `until`
resolver spoke only the bare `<step>.<field>` form — an SDK-rendered
`$steps.<id>.<field>` never resolved, so a converged loop still ran to
exhaustion.
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest
from pydantic import BaseModel, ConfigDict

from cairn.core.dispatch.dispatcher import RunDispatcher
from cairn.core.dispatch.pipeline_bridge import PipelineBridge
from cairn.core.engine.pipeline import parse_pipeline_def, plan_pipeline
from cairn.core.engine.pipeline_loop import evaluate_until
from cairn.core.operator import Operation, register_operator
from cairn.core.storage import build_storage
from cairn.core.storage.pipeline_store import InMemoryPipelineStore
from tests.core.test_trigger_server_dispatch import FakeRunner


class _P(BaseModel):
    model_config = ConfigDict(extra="allow")


@register_operator(name="_t1303/echo", version="0.1.0")
class _Echo(Operation[_P, _P]):
    input_schema = _P
    output_schema = _P

    def run(self, inp, ctx):
        return _P(echo=str(getattr(inp, "x", "")))


@pytest.fixture(autouse=True)
def _ops_registered():
    # the full suite resets the default registry (seen on #1278); re-pin
    from cairn.core.operator.registry import default_registry
    try:
        default_registry.register(_Echo, strict=False)
    except Exception:  # noqa: BLE001 — already registered is fine
        pass


def _def(until: str):
    return parse_pipeline_def(
        f"""
name: looped
kind: pipeline
steps:
  - id: seed
    task: _t1303/echo
    with: {{x: pass}}
  - loop: fix
    steps:
      - id: inner
        task: _t1303/echo
        with: {{x: fixing}}
    until: {until}
    max_iterations: 2
    depends_on: [seed]
""", pack="t1303", source="<test>")


async def _bridge():
    d = RunDispatcher(FakeRunner(), backend=build_storage())
    b = PipelineBridge(d, store=InMemoryPipelineStore())
    await b.start()
    return d, b


async def _wait(bridge, pid, states, timeout=8.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        rec = await bridge.store.get(pid)
        if rec.status in states:
            return rec
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(
                f"stuck in {rec.status!r}; steps="
                + str({k: v.status for k, v in rec.step_state.items()}))
        await asyncio.sleep(0.02)


def test_until_resolves_the_dollar_steps_form():
    outputs = {"seed": {"echo": "pass"}}
    assert evaluate_until("$steps.seed.echo == 'pass'", outputs)   # SDK render
    assert evaluate_until("seed.echo == 'pass'", outputs)          # bare form
    assert not evaluate_until("$steps.seed.echo == 'nope'", outputs)


async def test_satisfied_until_exits_after_one_iteration():
    d, b = await _bridge()
    try:
        plan = plan_pipeline(_def("$steps.seed.echo == 'pass'"))
        rec = await b.start_pipeline(plan, inputs={})
        rec = await _wait(b, rec.pipeline_id, ("done", "failed", "paused"))
        assert rec.status == "done", rec.status
        assert "inner" in rec.step_state
        assert "inner#2" not in rec.step_state    # converged: NO second iteration
    finally:
        b.stop()


async def test_second_iteration_runs_as_a_task_leg_then_exhausts_to_gate():
    """The append-path stamping: iteration 2 must EXECUTE (not die as an
    unknown template), and a non-converging loop still escalates to a human."""
    d, b = await _bridge()
    try:
        plan = plan_pipeline(_def("$steps.seed.echo == 'never'"))
        rec = await b.start_pipeline(plan, inputs={})
        rec = await _wait(b, rec.pipeline_id, ("paused", "failed"))
        assert rec.status == "paused", rec.status          # gate, not a crash
        # the exhaustion gate lands ON the tail step: its RUN completed (the
        # proof iteration 2 executed as a task leg), then the step became the
        # escalation carrier awaiting a human.
        tail = rec.step_state["inner#2"]
        assert tail.status == "awaiting_approval" and tail.verdict == "loop_exhausted"
        run2 = await d.store.get(tail.run_id)
        assert run2.status == "done"                       # iteration 2 RAN
        assert run2.template_ref == "task:_t1303/echo"     # the D0 stamp held
    finally:
        b.stop()
