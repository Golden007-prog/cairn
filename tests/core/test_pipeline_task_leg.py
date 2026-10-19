"""#1278 — the operator-leg executor: `task:` legs run as governed runs.

NO mocked compile/run path anywhere in this file: pipelines here use only
task + gate legs, so a REAL dispatcher, bridge, bus and operator registry
carry every test. This is the runtime half the SDK live E2E found missing —
lifted @step defs (#1254) and shared-op refs compile to task legs, and until
now `assert_runnable` refused them at admission.
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest
from pydantic import BaseModel, ConfigDict

from cairn.core.dispatch.dispatcher import RunDispatcher
from cairn.core.dispatch.leg_executor import leg_kind_of
from cairn.core.dispatch.pipeline_bridge import PipelineBridge, reconstruct_step_output
from cairn.core.engine.pipeline import parse_pipeline_def, plan_pipeline
from cairn.core.engine.pipeline_resolve import assert_runnable
from cairn.core.operator import (
    CapabilityNotDeclared,
    Operation,
    guard_context,
    register_operator,
)
from cairn.core.storage import build_storage
from cairn.core.storage.pipeline_store import InMemoryPipelineStore
from tests.core.test_trigger_server_dispatch import FakeRunner


class _P(BaseModel):
    model_config = ConfigDict(extra="allow")


@register_operator(name="_t1278/echo", version="0.1.0")
class _Echo(Operation[_P, _P]):
    """Adds one — output presence + wiring proof."""
    input_schema = _P
    output_schema = _P

    def run(self, inp, ctx):
        return _P(n_out=int(getattr(inp, "n", 0)) + 1, node=ctx.node_name)


@register_operator(name="_t1278/boom", version="0.1.0")
class _Boom(Operation[_P, _P]):
    input_schema = _P
    output_schema = _P

    def run(self, inp, ctx):
        raise RuntimeError("op exploded")


@register_operator(name="_t1278/greedy", version="0.1.0")
class _Greedy(Operation[_P, _P]):
    """Mimics a lifted @step op: ctx guarded to its requires, then oversteps."""
    input_schema = _P
    output_schema = _P
    requires: list = []

    def run(self, inp, ctx):
        g = guard_context(ctx, self.requires)
        return _P(secret=g.secrets.get("X"))          # secrets NOT declared


@pytest.fixture(autouse=True)
def _ops_registered():
    """Other suite tests reset/reload the default operator registry (the full
    run wiped the module-import registrations above — legs then failed with
    UnknownOperatorError). Re-pin ours before every test; same class, same
    name, so a re-register is a no-op when nothing reset."""
    from cairn.core.operator.registry import default_registry
    for cls in (_Echo, _Boom, _Greedy):
        try:
            default_registry.register(cls, strict=False)
        except Exception:  # noqa: BLE001 — already registered is fine
            pass


def _def(yaml_text: str):
    return parse_pipeline_def(yaml_text, pack="t1278", source="<test>")


_TWO_TASKS_GATED = """
name: tasks
kind: pipeline
steps:
  - id: first
    task: _t1278/echo
    with: {n: 41}
  - gate: approve
    depends_on: [first]
  - id: second
    task: _t1278/echo
    with: {n: first.n_out}
    depends_on: [approve]
"""


# ── grammar + plan + admission ──────────────────────────────────────────────

def test_leg_kind_of_task_prefix():
    assert leg_kind_of("task:_t1278/echo") == "task"
    assert leg_kind_of("flow:p/f") == "flow"          # unchanged
    assert leg_kind_of("p/template") == "template"    # unchanged


def test_plan_prefixes_task_refs():
    plan = plan_pipeline(_def(_TWO_TASKS_GATED))
    refs = {s.id: s.ref for s in plan.steps}
    assert refs["first"] == "task:_t1278/echo"
    assert refs["second"] == "task:_t1278/echo"


def test_assert_runnable_admits_task_legs():
    assert_runnable(_def(_TWO_TASKS_GATED))           # no NotImplementedError


# ── the executor, end to end on a real bridge ───────────────────────────────

async def _bridge():
    d = RunDispatcher(FakeRunner(), backend=build_storage())
    bridge = PipelineBridge(d, store=InMemoryPipelineStore())
    await bridge.start()
    return d, bridge


async def _wait_for(bridge, pid, states, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        rec = await bridge.store.get(pid)
        if rec.status in states:
            return rec
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(
                f"pipeline stuck in {rec.status!r}; steps="
                + str({k: v.status for k, v in rec.step_state.items()}))
        await asyncio.sleep(0.02)


async def test_task_legs_run_gate_pauses_wires_flow_through():
    """The #1278 acceptance spine: task leg → HITL gate → task leg, outputs
    wired through, every leg its own governed run."""
    d, bridge = await _bridge()
    try:
        plan = plan_pipeline(_def(_TWO_TASKS_GATED))
        rec = await bridge.start_pipeline(plan, inputs={})
        rec = await _wait_for(bridge, rec.pipeline_id, ("paused",))
        assert rec.step_state["first"].status == "done"

        # the first leg is a real governed run with a persisted output
        run1 = await d.store.get(rec.step_state["first"].run_id)
        assert run1.status == "done"
        assert run1.template_ref == "task:_t1278/echo"
        assert reconstruct_step_output(run1)["n_out"] == 42

        await bridge.advance_step(rec.pipeline_id, "approve", approver="dev")
        rec = await _wait_for(bridge, rec.pipeline_id, ("done", "failed"))
        assert rec.status == "done"

        # the wire `n: first.n_out` resolved to a VALUE, not a literal
        run2 = await d.store.get(rec.step_state["second"].run_id)
        assert run2.inputs["n"] == 42
        assert reconstruct_step_output(run2)["n_out"] == 43
        assert reconstruct_step_output(run2)["node"] == "second"
    finally:
        bridge.stop()


async def test_task_leg_failure_is_an_outcome_not_a_crash():
    d, bridge = await _bridge()
    try:
        plan = plan_pipeline(_def(
            "name: boom\nkind: pipeline\nsteps:\n  - id: only\n    task: _t1278/boom\n"))
        rec = await bridge.start_pipeline(plan, inputs={})
        rec = await _wait_for(bridge, rec.pipeline_id, ("failed",))
        run = await d.store.get(rec.step_state["only"].run_id)
        assert run.status == "failed"
        assert "op exploded" in (run.error or "")
    finally:
        bridge.stop()


async def test_unknown_operator_fails_the_leg_loud():
    d, bridge = await _bridge()
    try:
        plan = plan_pipeline(_def(
            "name: ghost\nkind: pipeline\nsteps:\n  - id: only\n    task: nope/missing\n"))
        rec = await bridge.start_pipeline(plan, inputs={})
        rec = await _wait_for(bridge, rec.pipeline_id, ("failed",))
        run = await d.store.get(rec.step_state["only"].run_id)
        assert "No operator registered" in (run.error or "")
    finally:
        bridge.stop()


async def test_capability_guard_fails_the_leg_at_runtime():
    """D5's runtime half through the bridge: an op overstepping its declared
    requires fails ITS leg (and the pipeline), with the capability named."""
    d, bridge = await _bridge()
    try:
        plan = plan_pipeline(_def(
            "name: greedy\nkind: pipeline\nsteps:\n  - id: only\n    task: _t1278/greedy\n"))
        rec = await bridge.start_pipeline(plan, inputs={})
        rec = await _wait_for(bridge, rec.pipeline_id, ("failed",))
        run = await d.store.get(rec.step_state["only"].run_id)
        assert "CapabilityNotDeclared" in (run.error or "")
        assert "ctx.secrets" in (run.error or "")
    finally:
        bridge.stop()


# ── the #1254 acceptance, finally true end-to-end ───────────────────────────

async def test_sdk_lifted_def_runs_as_a_task_leg(tmp_path):
    """Compile an SDK class with a lifted def, install its generated ops
    module (as the entry point would), and run the pipeline on the bridge:
    the acceptance line #1278 exists to make true."""
    import importlib.util

    from cairn.core.engine.pipeline import load_pipeline_def
    pytest.importorskip("cairn_sdk", reason="the authoring SDK is the platform's (ADR-0135)")
    from cairn_sdk import compile_pipeline, pipeline, step, gate

    @pipeline(series="live", pack="t1278-live")
    class Live:
        @step(requires=["artifacts"], risk="read_only")
        def fetch(self, ctx):
            return {"dataset": ctx.artifacts.put("raw", "mem://raw"), "rows": 3}

        approve = gate(approvers="me")

        @step(n=fetch.out.rows)
        def summarize(self, ctx, inputs):
            return {"summary": f"{inputs.get('n')} rows"}

        graph = fetch >> approve >> summarize

    compiled = compile_pipeline(Live)
    pack = tmp_path / "t1278-live"
    compiled.write(pack)
    mod_path = pack / "src" / compiled._pkg / f"{compiled._steps_module_name}.py"
    spec = importlib.util.spec_from_file_location("t1278_live_steps", mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                      # registers the lifted ops

    d, bridge = await _bridge()
    try:
        dfn = load_pipeline_def(pack / "pipelines" / "live.yaml")
        assert_runnable(dfn)
        rec = await bridge.start_pipeline(plan_pipeline(dfn), inputs={})
        rec = await _wait_for(bridge, rec.pipeline_id, ("paused",))
        await bridge.advance_step(rec.pipeline_id, "approve", approver="me")
        rec = await _wait_for(bridge, rec.pipeline_id, ("done", "failed"))
        assert rec.status == "done"
        run = await d.store.get(rec.step_state["summarize"].run_id)
        assert reconstruct_step_output(run)["summary"] == "3 rows"
    finally:
        bridge.stop()


# ── the enforcement point applies to task legs too (#1402) ──────────────────


@register_operator(name="_t1402/gated", version="0.1.0")
class _Gated(Operation[_P, _P]):
    """Declares nothing itself — the GATE comes from registry metadata, which
    is where an operator's `requires_approval` lives (pack.toml)."""
    input_schema = _P
    output_schema = _P

    def run(self, inp, ctx):
        return _P(ran=True)


def _gate_metadata(requires_approval: bool) -> None:
    """Attach/replace the manifest metadata `resolve_runtime_gate` reads."""
    from cairn.core.operator.registry import default_registry
    from cairn.core.pack.manifest import OperatorEntry

    if not default_registry.has("_t1402/gated"):
        default_registry.register(_Gated, strict=False)
    default_registry.attach_metadata(
        OperatorEntry(name="_t1402/gated", requires_approval=requires_approval)
    )


async def test_gated_operator_cannot_run_as_an_ungated_task_leg():
    """#1402 — the regression this exists to prevent.

    `task:` legs used to call `op.run` directly, so an operator declaring
    `requires_approval` executed with NO approval: gating became a property of
    whether the pipeline author wrote `approval: true`, not of the operator's
    declared risk. The guarantee under test is that the operator DOES NOT RUN
    without an approval.

    #1759 changed HOW that guarantee is kept, not whether: the leg used to
    FAIL, killing the pipeline (and wasting every leg already paid for) even
    though the answer was "a human must decide". It now PAUSES on
    `awaiting_approval` — the same outcome every other gate in the product
    produces, and releasable through the queue the sibling test below drives.
    That change is exactly the owner decision that test's docstring called for.
    """
    _gate_metadata(True)
    d, bridge = await _bridge()
    try:
        plan = plan_pipeline(_def(
            "name: ungated\nkind: pipeline\nsteps:\n  - id: only\n    task: _t1402/gated\n"))
        rec = await bridge.start_pipeline(plan, inputs={})
        # `paused` only: the pause happens INSIDE the leg body, so accepting
        # "running" here would return on the first poll and race the gate.
        rec = await _wait_for(bridge, rec.pipeline_id, ("paused",))
        assert rec.step_state["only"].status == "awaiting_approval"
        # THE #1402 guarantee, unchanged: the gated operator did not execute.
        run = await d.store.get(rec.step_state["only"].run_id)
        assert run.status != "done"
        assert not (run.result_payloads or []), "gated operator produced output"
    finally:
        _gate_metadata(False)
        bridge.stop()


async def test_step_approved_through_the_bridge_satisfies_the_operator_gate():
    """The other half: the gate must be SATISFIABLE, and by the approval queue
    that already exists — a step a human released carries its approver, and
    that decision is what clears the operator's declared gate. No second
    approval mechanism.

    NOTE the plan is built and then marked approval-gated directly: the plan
    model carries `approval` per step, but `plan_pipeline` only *derives* it
    for `gate:` legs, so the pipeline DEF grammar has no way to spell
    "this task step needs a human" (see the PR for #1402 — that grammar gap is
    what makes an operator-gated task leg fail-closed-and-unsatisfiable from a
    def today, and it needs an owner decision, not a silent workaround).
    """
    _gate_metadata(True)
    d, bridge = await _bridge()
    try:
        plan = plan_pipeline(_def(
            "name: gated\nkind: pipeline\nsteps:\n  - id: only\n    task: _t1402/gated\n"))
        plan.steps[0].approval = True          # what a def cannot yet express
        rec = await bridge.start_pipeline(plan, inputs={})
        rec = await _wait_for(bridge, rec.pipeline_id, ("paused", "running"))
        assert rec.step_state["only"].status == "awaiting_approval"

        await bridge.advance_step(rec.pipeline_id, "only", approver="alice")
        rec = await _wait_for(bridge, rec.pipeline_id, ("done",))
        run = await d.store.get(rec.step_state["only"].run_id)
        assert run.status == "done", run.error
        assert (run.result_payloads or [{}])[0]["output"]["ran"] is True
    finally:
        _gate_metadata(False)
        bridge.stop()


async def test_ungated_operator_still_runs_untouched():
    """The fix must not gate everything: an operator with no declared approval
    requirement runs as before."""
    d, bridge = await _bridge()
    try:
        plan = plan_pipeline(_def(
            "name: plain\nkind: pipeline\nsteps:\n  - id: only\n    task: _t1278/echo\n"))
        rec = await bridge.start_pipeline(plan, inputs={})
        rec = await _wait_for(bridge, rec.pipeline_id, ("done",))
        run = await d.store.get(rec.step_state["only"].run_id)
        assert run.status == "done", run.error
    finally:
        bridge.stop()
