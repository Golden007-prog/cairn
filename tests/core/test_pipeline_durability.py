"""Pipeline-tier durability: the RUN STORE is the checkpoint (#1130, ADR-0084 D6).

The claim this file exists to prove: **no Temporal is needed at the pipeline
tier**, because the durable state is already persisted — completed legs are
terminal runs with persisted outputs, and resume is plan re-evaluation rather
than replay.

That generalizes ADR-0083 D7 ("durability derived per segment") one tier up:

* **pipeline** — run store + plan (this file)
* **agentic leg** — LangGraph checkpointer / `interrupt()` (unchanged)
* **data/ml leg** — engine-owned resume; the control plane holds only the handle

Three restart scenarios, because those are the ways a control plane actually
dies: between legs, mid-leg, and mid-loop.
"""
from __future__ import annotations

import pytest

from cairn.core.dispatch.pipeline_resume import (
    ResumePlan,
    plan_resume,
    reattachable_legs,
)


class _Rec:
    """Minimal stand-in for a persisted PipelineRecord."""

    def __init__(self, steps: dict, cursor=None, status="running"):
        self.pipeline_id = "pl-1"
        self.status = status
        self.step_state = {k: _St(**v) for k, v in steps.items()}
        self.cursor = cursor or []


class _St:
    def __init__(self, status="pending", run_id="", handle=None, iteration=0):
        self.status, self.run_id, self.handle, self.iteration = (
            status, run_id, handle, iteration)


# ── restart 1: BETWEEN legs ──────────────────────────────────────────────────

def test_restart_between_legs_resumes_at_the_next_pending_leg():
    """The commonest case: leg A finished and was persisted, the process died
    before leg B was submitted. Nothing is replayed — A stays done."""
    rec = _Rec({"a": {"status": "done", "run_id": "r-a"},
                "b": {"status": "pending"}})
    plan = plan_resume(rec)
    assert isinstance(plan, ResumePlan)
    assert plan.resubmit == ["b"]
    assert plan.reattach == []
    assert "a" not in plan.resubmit, "a completed leg must never re-run"


def test_a_completed_pipeline_needs_no_resume():
    rec = _Rec({"a": {"status": "done"}, "b": {"status": "done"}}, status="done")
    assert plan_resume(rec).is_noop


def test_a_paused_gate_is_left_alone_on_restart():
    """A human still owes a decision — restarting must not auto-advance it, and
    must not re-ask either."""
    rec = _Rec({"a": {"status": "done"}, "g": {"status": "awaiting_approval"}})
    plan = plan_resume(rec)
    assert plan.resubmit == [] and plan.reattach == []
    assert plan.awaiting == ["g"]


# ── restart 2: MID-leg ───────────────────────────────────────────────────────

def test_restart_mid_leg_REATTACHES_rather_than_resubmitting():
    """THE property that makes Temporal unnecessary here. The engine kept
    working through the control-plane restart; re-submitting would duplicate
    paid compute. The handle is the reattach token."""
    rec = _Rec({"a": {"status": "running", "run_id": "r-a",
                      "handle": {"engine": "metaflow", "job_id": "j1"}}})
    plan = plan_resume(rec)
    assert plan.reattach == ["a"]
    assert plan.resubmit == [], "a running leg must NOT be resubmitted"


def test_a_running_leg_with_no_handle_cannot_reattach_and_is_flagged():
    """Honest failure: without a handle there is nothing to re-attach to, and
    silently resubmitting could double-charge. It becomes a decision, not a
    guess."""
    rec = _Rec({"a": {"status": "running", "run_id": "r-a", "handle": None}})
    plan = plan_resume(rec)
    assert plan.orphaned == ["a"]
    assert plan.reattach == [] and plan.resubmit == []


def test_reattachable_legs_reads_handles_from_the_record():
    rec = _Rec({"a": {"status": "running", "handle": {"engine": "builtin", "job_id": "1"}},
                "b": {"status": "done"}})
    assert [h["job_id"] for h in reattachable_legs(rec)] == ["1"]


# ── restart 3: MID-loop ──────────────────────────────────────────────────────

def test_restart_mid_loop_resumes_at_the_recorded_iteration():
    """A loop's position is persisted, so a restart neither restarts the loop
    from zero (wasted spend) nor skips ahead (unverified convergence)."""
    rec = _Rec({"remediate": {"status": "running", "iteration": 2}})
    plan = plan_resume(rec)
    assert plan.iterations == {"remediate": 2}


def test_a_loop_that_had_not_started_resumes_at_zero():
    rec = _Rec({"remediate": {"status": "pending", "iteration": 0}})
    assert plan_resume(rec).iterations.get("remediate", 0) == 0


# ── the tier claim ───────────────────────────────────────────────────────────

def test_resume_planning_imports_no_workflow_engine(monkeypatch):
    """ADR-0084 D6: the pipeline tier needs neither Temporal nor a graph runtime
    — the run store is the checkpoint. Asserted, not asserted-in-prose."""
    import sys

    for mod in [m for m in sys.modules if m.startswith(("temporalio", "langgraph"))]:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    plan_resume(_Rec({"a": {"status": "pending"}}))
    leaked = [m for m in sys.modules if m.startswith(("temporalio", "langgraph"))]
    assert not leaked, f"pipeline resume pulled in a workflow engine: {leaked}"


def test_the_plan_is_derived_not_stored():
    """Resume is plan RE-EVALUATION: same record in, same decision out. If it
    were stored state it could drift from the record it describes."""
    rec = _Rec({"a": {"status": "done"}, "b": {"status": "pending"}})
    assert plan_resume(rec).resubmit == plan_resume(rec).resubmit


@pytest.mark.parametrize("status,bucket", [
    ("pending", "resubmit"), ("queued", "resubmit"),
    ("awaiting_approval", "awaiting"), ("awaiting_advance", "awaiting"),
    ("done", None), ("skipped", None), ("failed", None),
])
def test_every_step_status_has_a_defined_resume_behaviour(status, bucket):
    """No status may fall through unhandled — an unclassified step on restart is
    a pipeline that silently stalls."""
    rec = _Rec({"x": {"status": status}})
    plan = plan_resume(rec)
    for name in ("resubmit", "reattach", "awaiting", "orphaned"):
        in_bucket = "x" in getattr(plan, name)
        assert in_bucket == (name == bucket), f"{status} → {name}={in_bucket}"
