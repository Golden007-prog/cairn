"""#1558 — the approval resume path under Temporal, hermetically.

Two defects found live on 2026-08-22:

  1. ``TemporalRuntime.resume`` reported success while the workflow history
     showed no ``WorkflowExecutionSignaled`` was ever appended — an approval
     that claims delivery it doesn't have. ``resume`` now VERIFIES the signal
     against the fetched history and raises when it isn't there, so the API
     502s instead of lying.
  2. A gated cross-pack subgraph under ``OBS_RUNTIME=temporal`` runs as ONE
     activity; its inner ``interrupt()`` cannot resume (the activity's
     in-process checkpoint dies with the invocation), so every approval loops
     back to the same gate. The flatten emitter (#569/#581) was built for
     exactly this and gated on the in-process equivalence suite — which
     passes — so it is now the DEFAULT under temporal.

The signal tests run against ``temporalio.testing.WorkflowEnvironment`` —
the same no-cluster harness ``test_pipeline_workflow.py`` uses — with a
minimal workflow that mirrors ``TemplateWorkflow``'s signal contract
(``approve`` signal + ``wait_condition``). The full plugin-executed graph
still needs a live-box acceptance (#1529's remaining rows).
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from typing import Any

import pytest
from temporalio import workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from cairn.core.runtime.temporal.runtime import TemporalRuntime


@workflow.defn(name="t1558.PausingWorkflow")
class PausingWorkflow:
    """Minimal mirror of TemplateWorkflow's approval contract: block on the
    ``approve`` signal, return the delivered decision."""

    def __init__(self) -> None:
        self._decision: dict[str, Any] | None = None

    @workflow.signal
    def approve(self, decision: dict[str, Any]) -> None:
        self._decision = decision

    @workflow.run
    async def run(self) -> dict[str, Any]:
        await workflow.wait_condition(lambda: self._decision is not None)
        return dict(self._decision or {})


# The sandbox re-imports this module for the workflow; cairn and the test
# package are deterministic here — same passthrough test_pipeline_workflow uses.
_RUNNER = SandboxedWorkflowRunner(
    restrictions=SandboxRestrictions.default.with_passthrough_modules("cairn", "tests"))


def _runtime_for(env: WorkflowEnvironment) -> TemporalRuntime:
    """A TemporalRuntime whose OWN client points at the test server — the same
    connect path the server process takes, not the env's premade client."""
    cfg = env.client.service_client.config
    rt = TemporalRuntime(address=cfg.target_host, namespace=env.client.namespace)
    return rt


async def test_resume_delivers_and_verifies_the_signal():
    """The happy path, end to end: resume() signals, the history VERIFIABLY
    carries the signal, and the paused workflow completes with the decision."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="t1558", workflows=[PausingWorkflow],
                          workflow_runner=_RUNNER):
            handle = await env.client.start_workflow(
                PausingWorkflow.run, id="run-t1558-happy", task_queue="t1558",
            )
            record = {"approved": True, "approver": "t1558", "channel": "test"}
            await _runtime_for(env).resume(run_id="run-t1558-happy", decision=record)
            result = await handle.result()
            assert result == record


async def test_resume_to_a_missing_workflow_raises_not_lies():
    """A signal aimed at nothing must surface as an error (the API turns it
    into a 502) — never a 200 `resumed: true`."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        with pytest.raises(Exception):  # noqa: B017 — RPC NotFound or our RuntimeError; either is loud
            await _runtime_for(env).resume(
                run_id="run-t1558-ghost", decision={"approved": True},
            )


async def test_verification_counts_the_actual_history_events():
    """The verification half in isolation: after resume(), the fetched history
    contains exactly the signal we sent (delivery is evidence, not optimism)."""
    from temporalio.api.enums.v1 import EventType

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="t1558", workflows=[PausingWorkflow],
                          workflow_runner=_RUNNER):
            handle = await env.client.start_workflow(
                PausingWorkflow.run, id="run-t1558-count", task_queue="t1558",
            )
            await _runtime_for(env).resume(
                run_id="run-t1558-count", decision={"approved": False},
            )
            history = await handle.fetch_history()
            signals = [
                e for e in history.events
                if e.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED
            ]
            assert len(signals) == 1
            await handle.result()


# ── flatten is the default under temporal (#1558 defect 2) ──────────────


def test_flatten_defaults_on_under_temporal(monkeypatch):
    from cairn.core.engine.compiler import _flatten_composition_enabled

    monkeypatch.delenv("OBS_FLATTEN_COMPOSITION", raising=False)
    monkeypatch.setenv("OBS_RUNTIME", "temporal")
    assert _flatten_composition_enabled() is True

    # in-process keeps the byte-identical nested path by default
    monkeypatch.setenv("OBS_RUNTIME", "in_process")
    assert _flatten_composition_enabled() is False

    # the env override still wins in BOTH directions
    monkeypatch.setenv("OBS_RUNTIME", "temporal")
    monkeypatch.setenv("OBS_FLATTEN_COMPOSITION", "0")
    assert _flatten_composition_enabled() is False
    monkeypatch.setenv("OBS_RUNTIME", "in_process")
    monkeypatch.setenv("OBS_FLATTEN_COMPOSITION", "1")
    assert _flatten_composition_enabled() is True
