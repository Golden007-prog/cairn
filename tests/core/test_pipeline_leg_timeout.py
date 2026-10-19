"""#1288 — leg `timeout:` carried record → plan → runtime, and the 3600s
poll-budget bug: before this, the plan DROPPED the field and every pipeline
flow leg ran under execute_leg's 3600s default — whose expiry CANCELS the
engine job, killing any >1h leg at the hour mark.
"""
from __future__ import annotations

import asyncio
import os
import time

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest
from pydantic import BaseModel, ConfigDict

import cairn.core.dispatch.pipeline_bridge as bridge_mod
from cairn.core.dispatch.dispatcher import RunDispatcher
from cairn.core.dispatch.leg_executor import LegOutcome
from cairn.core.dispatch.pipeline_bridge import PipelineBridge
from cairn.core.engine.pipeline import (
    PipelineDefError,
    parse_pipeline_def,
    plan_pipeline,
    timeout_seconds,
)
from cairn.core.operator import Operation, register_operator
from cairn.core.storage import build_storage
from cairn.core.storage.pipeline_store import InMemoryPipelineStore
from tests.core.test_trigger_server_dispatch import FakeRunner


class _P(BaseModel):
    model_config = ConfigDict(extra="allow")


@register_operator(name="_t1288/sleepy", version="0.1.0")
class _Sleepy(Operation[_P, _P]):
    input_schema = _P
    output_schema = _P

    def run(self, inp, ctx):
        time.sleep(float(getattr(inp, "sleep_s", 0.0)))
        return _P(ok=True)


@pytest.fixture(autouse=True)
def _ops_registered():
    # the full suite resets the default registry (seen on #1278); re-pin
    from cairn.core.operator.registry import default_registry
    try:
        default_registry.register(_Sleepy, strict=False)
    except Exception:  # noqa: BLE001 — already registered is fine
        pass


def _def(yaml_text: str):
    return parse_pipeline_def(yaml_text, pack="t1288", source="<test>")


# ── the ONE duration grammar ────────────────────────────────────────────────

def test_duration_parses_the_documented_forms():
    assert timeout_seconds("8h") == 8 * 3600
    assert timeout_seconds("30m") == 1800
    assert timeout_seconds("90s") == 90
    assert timeout_seconds("2d") == 2 * 86400
    assert timeout_seconds("1.5h") == 5400
    assert timeout_seconds("3600") == 3600
    assert timeout_seconds(None) is None
    assert timeout_seconds("") is None


@pytest.mark.parametrize("bad", ["8 hours", "15min", "h8", "soon"])
def test_duration_typos_fail_loud(bad):
    with pytest.raises(PipelineDefError, match="unparseable timeout"):
        timeout_seconds(bad)


def test_timeout_typo_fails_at_load_not_at_runtime():
    with pytest.raises(PipelineDefError, match="unparseable timeout"):
        _def("name: x\nkind: pipeline\nsteps:\n"
             "  - id: a\n    task: _t1288/sleepy\n    timeout: 15min\n")


def test_gate_timeout_is_the_approval_surface_not_a_duration():
    # `timeout: 7d` on a HITL gate is approval expiry — untouched by #1288
    d = _def("name: g\nkind: pipeline\nsteps:\n"
             "  - id: a\n    task: _t1288/sleepy\n"
             "  - gate: ok\n    approvers: me\n    timeout: 7d\n"
             "    depends_on: [a]\n")
    plan = plan_pipeline(d)
    g = next(s for s in plan.steps if s.ref == "gate:ok")
    assert g.timeout_s is None


# ── plan carriage ───────────────────────────────────────────────────────────

def test_plan_carries_leg_timeout_in_seconds():
    plan = plan_pipeline(_def(
        "name: t\nkind: pipeline\nsteps:\n"
        "  - id: a\n    task: _t1288/sleepy\n    timeout: 2h\n"))
    assert plan.steps[0].timeout_s == 7200.0


# ── flow legs: the declared timeout IS the poll budget ──────────────────────

async def _bridge():
    d = RunDispatcher(FakeRunner(), backend=build_storage())
    bridge = PipelineBridge(d, store=InMemoryPipelineStore())
    await bridge.start()
    return d, bridge


async def _wait_for(bridge, pid, states, timeout=8.0):
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


async def test_flow_leg_poll_budget_is_the_declared_timeout(monkeypatch, tmp_path):
    """The regression at the heart of #1288: the bridge must hand execute_leg
    the DECLARED timeout — and a generous default, never 3600 — as the poll
    budget. Captured at the call seam; the def carries a real flow leg."""
    captured: list[float] = []

    async def _fake_execute_leg(ref, **kw):
        captured.append(kw.get("poll_budget_s"))
        return LegOutcome(state="succeeded", stats={}, metrics={})

    monkeypatch.setattr(bridge_mod, "execute_leg", _fake_execute_leg)
    monkeypatch.setattr(PipelineBridge, "_leg_engine", lambda self, s: object())

    d, bridge = await _bridge()
    try:
        for body, want in [
            ("name: f1\nkind: pipeline\nsteps:\n"
             "  - id: a\n    flow: t1288/clean\n    timeout: 8h\n", 8 * 3600.0),
            ("name: f2\nkind: pipeline\nsteps:\n"
             "  - id: a\n    flow: t1288/clean\n",
             bridge_mod._FLOW_LEG_DEFAULT_BUDGET_S),
        ]:
            rec = await bridge.start_pipeline(plan_pipeline(_def(body)), inputs={})
            rec = await _wait_for(bridge, rec.pipeline_id, ("done", "failed"))
            assert rec.status == "done"
        assert captured == [8 * 3600.0, 7 * 86400.0]
        assert 3600.0 not in captured           # the old silent cap is gone
    finally:
        bridge.stop()


# ── task legs: wait_for around the op ───────────────────────────────────────

async def test_task_leg_times_out_as_a_failed_outcome():
    d, bridge = await _bridge()
    try:
        plan = plan_pipeline(_def(
            "name: slow\nkind: pipeline\nsteps:\n"
            "  - id: a\n    task: _t1288/sleepy\n    timeout: 1s\n"
            "    with: {sleep_s: 3.0}\n"))
        rec = await bridge.start_pipeline(plan, inputs={})
        rec = await _wait_for(bridge, rec.pipeline_id, ("failed",))
        run = await d.store.get(rec.step_state["a"].run_id)
        assert "declared timeout" in (run.error or "")
    finally:
        bridge.stop()


async def test_task_leg_with_generous_timeout_completes():
    d, bridge = await _bridge()
    try:
        plan = plan_pipeline(_def(
            "name: fast\nkind: pipeline\nsteps:\n"
            "  - id: a\n    task: _t1288/sleepy\n    timeout: 30s\n"
            "    with: {sleep_s: 0.05}\n"))
        rec = await bridge.start_pipeline(plan, inputs={})
        rec = await _wait_for(bridge, rec.pipeline_id, ("done", "failed"))
        assert rec.status == "done"
    finally:
        bridge.stop()
