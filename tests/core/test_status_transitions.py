"""ADR-0086 H3 (#1245) — the status-transition guard, enforced in the stores.

The test names follow Prefect's orchestration-rule inventory (core_policy.py)
deliberately: their ~31 rules are the production failure-mode checklist this
guard is sized against. Each test pins one rule cairn now enforces at the ONE
choke point every writer goes through (runs-store ``update()`` / pipeline-store
``put()``), so the #1241 / #977 / #1239 bug class is mechanically impossible
rather than review-caught.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.storage import build_storage
from cairn.core.storage.pipeline_store import InMemoryPipelineStore, PipelineRecord, StepState
from cairn.core.storage.protocols import Run, TriggerEvent
from cairn.core.storage.transitions import (
    check_transition,
    guard_pipeline_put,
    guard_run_update,
)


def _run(run_id="r1", status="queued") -> Run:
    return Run(id=run_id, trigger=TriggerEvent(source="manual", payload={}), status=status)


async def _store_with(status: str):
    store = build_storage().runs
    await store.create(_run(status=status))
    return store


# ── pure table checks ────────────────────────────────────────────────────────

def test_handle_flow_terminal_state_transitions():
    """Terminal states are immutable — Prefect: HandleFlowTerminalStateTransitions."""
    for terminal in ("done", "failed", "cancelled", "expired"):
        for to in ("queued", "running", "awaiting_approval", "done", "failed", "cancelled"):
            if to == terminal:
                continue
            assert not check_transition("run", terminal, to), f"{terminal} → {to} must be illegal"


def test_prevent_duplicate_transitions():
    """Re-writing the SAME status is a legal idempotent no-op — Prefect:
    PreventDuplicateTransitions (idempotent completions racing are benign)."""
    for s in ("queued", "running", "done", "failed", "cancelled"):
        assert check_transition("run", s, s)
        assert guard_run_update(s, {"status": s})


def test_prevent_backward_transitions():
    """No status may regress toward queued — Prefect: PreventPendingTransitions."""
    assert not check_transition("run", "running", "queued")
    assert not check_transition("run", "awaiting_approval", "queued")
    assert not check_transition("pipeline", "running", "queued")
    assert not check_transition("pipeline", "paused", "queued")


def test_expired_only_from_awaiting_approval():
    """#896 — the approval reaper's TTL expiry has exactly one legal source."""
    assert check_transition("run", "awaiting_approval", "expired")
    for s in ("queued", "running", "done", "failed", "cancelled"):
        assert not check_transition("run", s, "expired")


def test_pause_resume_cycle_is_legal():
    """HITL pause/resume — Prefect: HandlePausingFlows/HandleResumingPausedFlows."""
    assert check_transition("run", "running", "awaiting_approval")
    assert check_transition("run", "awaiting_approval", "running")
    assert check_transition("pipeline", "running", "paused")
    assert check_transition("pipeline", "paused", "running")


def test_unknown_current_is_permissive_unknown_target_is_not():
    """Forward/backward compat: a row written by another version must not be
    bricked (unknown CURRENT passes); nothing may transition INTO a status this
    version can't name (unknown TARGET refused)."""
    assert check_transition("run", "some_future_status", "failed")
    assert not check_transition("run", "running", "some_future_status")


def test_non_status_updates_always_pass():
    assert guard_run_update("done", {"error": "x", "ended_at": "t"})


# ── enforcement at the runs-store choke point (all backends via build_storage) ─

async def test_crash_belt_cannot_fail_a_cancelled_run():
    """The #1239-adjacent race: a late crash-belt write must not overwrite a
    human's cancel. Refusal is WHOLE-update (no partial ended_at stamp)."""
    store = await _store_with("cancelled")
    out = await store.update("r1", status="failed", error="late crash belt")
    assert out.status == "cancelled"
    assert out.error != "late crash belt", "refusal must drop the whole update"
    assert (await store.get("r1")).status == "cancelled"


async def test_done_run_cannot_be_reanimated():
    store = await _store_with("done")
    out = await store.update("r1", status="running")
    assert out.status == "done"


async def test_legal_lifecycle_still_flows():
    """queued → running → awaiting_approval → running → done, with field
    updates riding along — the guard must be invisible to correct code."""
    store = await _store_with("queued")
    assert (await store.update("r1", status="running", started_at="t0")).status == "running"
    assert (await store.update("r1", status="awaiting_approval")).status == "awaiting_approval"
    assert (await store.update("r1", status="running")).status == "running"
    out = await store.update("r1", status="done", ended_at="t1")
    assert out.status == "done" and out.ended_at == "t1"


# ── enforcement at the pipeline-store choke point ────────────────────────────

def _pipeline(status="running") -> PipelineRecord:
    return PipelineRecord(
        pipeline_id="pl1", bundle_ref="demo@0.1.0", status=status,
        step_state={"a": StepState(status="pending")},
    )


async def test_terminal_pipeline_put_is_refused_whole():
    """The #1241 shape inverted: once cancelled, no later advance/settle may
    resurrect the record — Prefect: EnforceCancellingToCancelledTransition."""
    store = InMemoryPipelineStore()
    await store.put(_pipeline(status="cancelled"))
    zombie = _pipeline(status="running")
    zombie.step_state["a"] = StepState(status="running")
    out = await store.put(zombie)
    assert out.status == "cancelled"
    assert out.step_state["a"].status == "pending", "refusal must drop the WHOLE put"


async def test_paused_to_running_put_is_legal():
    """The exact #1241 fix path: gate approval resumes a paused pipeline."""
    store = InMemoryPipelineStore()
    await store.put(_pipeline(status="paused"))
    assert (await store.put(_pipeline(status="running"))).status == "running"


async def test_same_status_put_carries_mutations():
    """Non-status mutations (step_state, outputs) re-put constantly — the guard
    must never block a self-transition put."""
    store = InMemoryPipelineStore()
    await store.put(_pipeline(status="running"))
    rec = _pipeline(status="running")
    rec.step_state["a"] = StepState(status="done")
    out = await store.put(rec)
    assert out.step_state["a"].status == "done"


def test_guard_pipeline_put_create_is_always_legal():
    assert guard_pipeline_put(None, "running", pipeline_id="new")
    assert guard_pipeline_put(None, "queued", pipeline_id="new")


def test_read_model_forward_jumps_are_legal():
    """The Temporal mirror (`pipeline_mirror_state`) writes whatever state the
    workflow is authoritatively in — a short pipeline can jump queued → done
    without the store ever observing `running`. Forward jumps pass; only
    regressions and terminal mutations are illegal."""
    assert check_transition("pipeline", "queued", "done")
    assert check_transition("pipeline", "queued", "paused")
    assert not check_transition("pipeline", "done", "queued")
    assert not check_transition("pipeline", "done", "running")
