"""#1361 — the temporal pipeline owner runs INDEPENDENT steps concurrently.

Before this, the workflow walked the topo order awaiting each leg to terminal
before the next — two independent legs paid wall-clock sum (the in-process
bridge has always overlapped them). The headline test is constructed so the
OLD sequential owner cannot pass it: step a's run does not finish until step
b has been SUBMITTED, which under a sequential walk never happens.

Hermetic under ``temporalio.testing.WorkflowEnvironment`` (time-skipping, no
cluster), same harness shape as test_pipeline_workflow.py — whose mocks'
signature drift (#1198's 6th SUBMIT arg) made every test there HANG in
infinite activity retries; keep mock signatures in lockstep with
``pipeline_activities.py``.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("temporalio")

from temporalio import activity  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402
from temporalio.worker.workflow_sandbox import (  # noqa: E402
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from cairn.core.engine.bundle import BundlePipeline, BundlePipelineStep  # noqa: E402
from cairn.core.engine.bundle_pipeline import plan_bundle  # noqa: E402
from cairn.core.runtime.temporal.pipeline_workflow import PipelineWorkflow  # noqa: E402

ORDER: list[str] = []
_B_SUBMITTED = asyncio.Event()
_FAIL_STEPS: set[str] = set()


@activity.defn(name="cairn.pipeline_prepare_step")
async def prep(step: dict, inputs: dict, outputs: dict) -> dict:
    return {"gate_passed": True, "routed_on": True, "resolved": {}}


@activity.defn(name="cairn.pipeline_submit_step")
async def submit(ref: str, resolved: dict, pipeline_id: str, step_id: str,
                 environment, step: dict | None = None) -> str:
    ORDER.append(f"submit:{step_id}")
    if step_id == "b":
        _B_SUBMITTED.set()
    return f"run_{step_id}"


@activity.defn(name="cairn.pipeline_await_run")
async def await_run(run_id: str) -> dict:
    sid = run_id[len("run_"):]
    if sid in _FAIL_STEPS:
        ORDER.append(f"failed:{sid}")
        return {"status": "failed", "verdict": "boom", "output": {}}
    if sid == "a":
        # a finishes ONLY once b was submitted. A sequential owner never
        # submits b while a runs → this times out and surfaces as the verdict
        # below instead of a silent test hang.
        try:
            await asyncio.wait_for(_B_SUBMITTED.wait(), timeout=45)
        except (TimeoutError, asyncio.TimeoutError):
            return {"status": "failed",
                    "verdict": "SEQUENTIAL: b was never submitted while a ran"}
    ORDER.append(f"done:{sid}")
    return {"status": "done", "verdict": "pass", "output": {}}


@activity.defn(name="cairn.pipeline_mirror_state")
async def mirror(pipeline_id: str, step_state: dict, status: str) -> None:
    return None


@activity.defn(name="cairn.pipeline_emit_event")
async def emit(event: str, output: dict, pipeline_id: str, step_id: str) -> None:
    return None


_ACTIVITIES = [prep, submit, await_run, mirror, emit]


def _plan(*steps: BundlePipelineStep) -> dict:
    return plan_bundle(BundlePipeline(name="pl", step=list(steps))).model_dump()


async def _run(plan: dict) -> dict:
    runner = SandboxedWorkflowRunner(
        restrictions=SandboxRestrictions.default.with_passthrough_modules("cairn"))
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="pl-par-tq",
                          workflows=[PipelineWorkflow], activities=_ACTIVITIES,
                          workflow_runner=runner):
            return await env.client.execute_workflow(
                PipelineWorkflow.run, args=[plan, {}, "pl_par"],
                id=f"wf-par-{len(ORDER)}", task_queue="pl-par-tq")


def _reset():
    ORDER.clear()
    _FAIL_STEPS.clear()
    _B_SUBMITTED.clear()


@pytest.mark.asyncio
async def test_independent_steps_overlap():
    """a cannot finish until b is submitted — impossible under the old
    sequential walk, trivial once independent branches overlap."""
    _reset()
    # DIAMOND: plan_bundle implicitly CHAINS steps with no declared deps
    # (bundle pipelines are chains by default), so genuinely-parallel branches
    # are authored as explicit same-parent deps — found while writing this
    # test: a "parallel" plan without the root was silently a chain.
    res = await _run(_plan(
        BundlePipelineStep(id="r", pack="p", template="r"),
        BundlePipelineStep(id="a", pack="p", template="a", depends_on=["r"]),
        BundlePipelineStep(id="b", pack="p", template="b", depends_on=["r"]),
        BundlePipelineStep(id="c", pack="p", template="c", depends_on=["a", "b"]),
    ))
    assert res["status"] == "done", res["step_state"]
    assert {s: st["status"] for s, st in res["step_state"].items()} == {
        "r": "done", "a": "done", "b": "done", "c": "done"}
    # the join step ran strictly after BOTH parents
    assert ORDER.index("submit:c") > ORDER.index("done:a")
    assert ORDER.index("submit:c") > ORDER.index("done:b")
    # and the overlap itself: b submitted while a was still running
    assert ORDER.index("submit:b") < ORDER.index("done:a")


@pytest.mark.asyncio
async def test_failed_leg_skips_dependents_but_running_sibling_finishes():
    """A failure stops LAUNCHING (dependents skip), while an already-running
    independent leg completes and records honestly — the in-process bridge's
    semantics, now shared by the durable owner."""
    _reset()
    _FAIL_STEPS.add("x")
    res = await _run(_plan(
        BundlePipelineStep(id="r", pack="p", template="r"),
        BundlePipelineStep(id="x", pack="p", template="x", depends_on=["r"]),
        BundlePipelineStep(id="b", pack="p", template="b", depends_on=["r"]),
        BundlePipelineStep(id="child", pack="p", template="child", depends_on=["x"]),
    ))
    assert res["status"] == "failed"
    st = {s: v["status"] for s, v in res["step_state"].items()}
    assert st["x"] == "failed"
    assert st["child"] == "skipped", "never-launched dependents resolve, not linger pending"
    assert st["b"] == "done", "an already-submitted independent leg records its real outcome"
