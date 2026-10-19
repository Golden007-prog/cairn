"""ADR-0086 H5 (#1247) — per-target concurrency cap.

`max_in_flight` on a compute connection caps how many legs may be IN FLIGHT on
that target. Excess legs WAIT in `queued` — a visible status, not a refusal —
and start when a slot frees. The count is derived from the run store (runs
stamped with the target, in a slot-holding status), so it needs no lease
bookkeeping and survives a restart.

Hermetic: stub engine + stubbed flow resolution + an in-memory connection store;
the real dispatcher, bus, bridge and run store.
"""
from __future__ import annotations

import asyncio
import os
import types

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import cairn.core.connections as connections_mod
import cairn.core.dispatch.pipeline_bridge as bridge_mod
from cairn.core.connections import Connection, InMemoryConnectionStore
from cairn.core.dataflow.contract import StageRef
from cairn.core.dispatch.dispatcher import RunDispatcher
from cairn.core.dispatch.pipeline_bridge import PipelineBridge
from cairn.core.dispatch.target_concurrency import in_flight_on_target, target_cap
from cairn.core.engine.bundle_pipeline import PipelinePlan, PipelinePlanStep
from cairn.core.storage import build_storage
from cairn.core.storage.pipeline_store import InMemoryPipelineStore
from cairn.core.storage.protocols import Run
from cairn.core.engine.schema import TriggerEvent
from tests.core.test_trigger_server_dispatch import FakeRunner

TARGET = "gpu-box"


# ── harness ────────────────────────────────────────────────────────────────


class _GatedEngine:
    """Reports `running` until released, and records every submit — the leg that
    was never admitted is the one whose spec never shows up here."""

    name = "stub"

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.submitted: list[str] = []

    def validate(self, spec):
        return []

    async def submit(self, spec, ctx):
        self.submitted.append(spec.dataflow_ref)
        return types.SimpleNamespace(job_id=f"j{len(self.submitted)}", engine=self.name, meta={})

    async def poll(self, handle):
        return types.SimpleNamespace(
            state="succeeded" if self.release.is_set() else "running",
            metrics={}, error="", retriable=False)

    async def cancel(self, handle):
        self.release.set()

    async def result(self, handle):
        return types.SimpleNamespace(stats={"clean.output": "s3://cleaned"})


def _stub_flow_resolution(monkeypatch):
    stage = StageRef(stage="clean.run")
    monkeypatch.setattr("cairn.core.dataflow.defs.load_flow_defs", lambda: {})
    monkeypatch.setattr("cairn.core.dataflow.defs.resolve_flow_ref",
                        lambda ref, cfg, kind="data": [stage])
    monkeypatch.setattr("cairn.core.dispatch.leg_executor._POLL_INTERVAL_S", 0.01)
    # the slot wait's polling floor — the bus event is the fast path, but the
    # cancel test deliberately exercises the poll.
    monkeypatch.setattr(bridge_mod, "_SLOT_POLL_MIN_S", 0.02)
    monkeypatch.setattr(bridge_mod, "_SLOT_POLL_MAX_S", 0.05)


def _connections(monkeypatch, *conns: Connection) -> InMemoryConnectionStore:
    store = InMemoryConnectionStore()
    for c in conns:
        store._by_id[c.id] = c
    monkeypatch.setattr(connections_mod, "default_connection_store", lambda: store)
    return store


def _compute(name: str, **kw) -> Connection:
    return Connection(name=name, kind="compute.ssh", **kw)


def _plan(*step_ids: str) -> PipelinePlan:
    return PipelinePlan(name="pl", steps=[
        PipelinePlanStep(id=sid, ref=f"flow:demo/{sid}") for sid in step_ids
    ])


async def _wait_for(predicate, *, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if await predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def _bridge(monkeypatch, engine, *, targets=None):
    """A started bridge whose legs resolve to `targets[step_id]` (default TARGET)."""
    monkeypatch.setattr(PipelineBridge, "_leg_engine", lambda self, s: engine)
    monkeypatch.setattr(PipelineBridge, "_leg_target",
                        lambda self, s: (targets or {}).get(s.id, TARGET))
    d = RunDispatcher(FakeRunner(), backend=build_storage())
    bridge = PipelineBridge(d, store=InMemoryPipelineStore())
    await bridge.start()
    return d, bridge


# ── the cap ────────────────────────────────────────────────────────────────


async def test_second_leg_waits_queued_and_starts_when_the_first_terminalizes(monkeypatch):
    """cap=1, two legs on one target: the second sits `queued` with NO engine
    submit, then runs once the first terminalizes — the pipeline completes."""
    _stub_flow_resolution(monkeypatch)
    _connections(monkeypatch, _compute(TARGET, max_in_flight=1))
    engine = _GatedEngine()
    d, bridge = await _bridge(monkeypatch, engine)
    try:
        rec = await bridge.start_pipeline(_plan("a", "b"), inputs={}, bundle_ref="demo@0.1.0")

        assert rec.step_state["a"].status == "running"
        assert rec.step_state["b"].status == "queued", "leg b was not held at the cap"
        run_b = await d.store.get(rec.step_state["b"].run_id)
        assert run_b.status == "queued" and run_b.compute_target == TARGET
        assert run_b.started_at is None, "a queued leg has not started"
        # the cap is a WAIT, not a refusal: nothing failed, and only one leg
        # ever reached the engine.
        await asyncio.sleep(0.1)
        assert engine.submitted == ["demo/a"], engine.submitted

        engine.release.set()
        assert await _wait_for(
            lambda: _pipeline_done(bridge, rec.pipeline_id)), "pipeline never finished"
        final = await bridge.store.get(rec.pipeline_id)
        assert final.status == "done"
        assert final.step_state["b"].status == "done"
        assert sorted(engine.submitted) == ["demo/a", "demo/b"]
        run_b = await d.store.get(final.step_state["b"].run_id)
        assert run_b.status == "done" and run_b.started_at, "admission never stamped a start"
    finally:
        bridge.stop()


async def _pipeline_done(bridge, pid) -> bool:
    rec = await bridge.store.get(pid)
    return bool(rec and rec.status in ("done", "failed", "cancelled"))


async def test_queued_leg_is_skipped_cleanly_when_the_pipeline_is_cancelled(monkeypatch):
    """Cancel while a leg waits for a slot → the leg is terminalized without ever
    touching the engine. `engine.submit` is the witness."""
    _stub_flow_resolution(monkeypatch)
    _connections(monkeypatch, _compute(TARGET, max_in_flight=1))
    engine = _GatedEngine()
    d, bridge = await _bridge(monkeypatch, engine)
    try:
        rec = await bridge.start_pipeline(_plan("a", "b"), inputs={}, bundle_ref="demo@0.1.0")
        b_run_id = rec.step_state["b"].run_id
        assert rec.step_state["b"].status == "queued"

        await bridge.cancel_pipeline(rec.pipeline_id)

        async def _b_cancelled():
            run = await d.store.get(b_run_id)
            return run is not None and run.status == "cancelled"

        assert await _wait_for(_b_cancelled), "the queued leg was never terminalized"
        assert engine.submitted == ["demo/a"], (
            f"a cancelled queued leg reached the engine: {engine.submitted}")
        final = await bridge.store.get(rec.pipeline_id)
        assert final.step_state["b"].status == "skipped"
        assert final.step_state["b"].verdict == "cancelled"
        engine.release.set()
    finally:
        bridge.stop()


async def test_no_cap_set_never_queues(monkeypatch):
    """A target whose connection sets no `max_in_flight` behaves exactly as
    before this slice: both legs are admitted at once."""
    _stub_flow_resolution(monkeypatch)
    _connections(monkeypatch, _compute(TARGET))  # no max_in_flight
    engine = _GatedEngine()
    d, bridge = await _bridge(monkeypatch, engine)
    try:
        rec = await bridge.start_pipeline(_plan("a", "b"), inputs={}, bundle_ref="demo@0.1.0")
        assert rec.step_state["a"].status == "running"
        assert rec.step_state["b"].status == "running"
        assert await _wait_for(lambda: _submitted_both(engine))
        engine.release.set()
        assert await _wait_for(lambda: _pipeline_done(bridge, rec.pipeline_id))
        assert (await bridge.store.get(rec.pipeline_id)).status == "done"
    finally:
        bridge.stop()


async def _submitted_both(engine) -> bool:
    return len(engine.submitted) == 2


async def test_no_target_skips_the_cap_machinery_entirely(monkeypatch):
    """The zero-change default: with no `<kind>_target` selected there is no
    connection lookup at all, and nothing is ever stamped or queued."""
    _stub_flow_resolution(monkeypatch)

    def _boom():
        raise AssertionError("the connection store must not be touched with no target")

    monkeypatch.setattr(connections_mod, "default_connection_store", _boom)
    engine = _GatedEngine()
    engine.release.set()
    d, bridge = await _bridge(monkeypatch, engine, targets={"a": "", "b": ""})
    try:
        rec = await bridge.start_pipeline(_plan("a"), inputs={}, bundle_ref="demo@0.1.0")
        assert rec.step_state["a"].status == "running"
        run = await d.store.get(rec.step_state["a"].run_id)
        assert run.compute_target == ""
        assert await _wait_for(lambda: _pipeline_done(bridge, rec.pipeline_id))
    finally:
        bridge.stop()


async def test_different_targets_do_not_block_each_other(monkeypatch):
    """The cap is PER target: two legs capped at 1 each, on different boxes, run
    together."""
    _stub_flow_resolution(monkeypatch)
    _connections(monkeypatch,
                 _compute("box-a", max_in_flight=1),
                 _compute("box-b", max_in_flight=1))
    engine = _GatedEngine()
    d, bridge = await _bridge(monkeypatch, engine, targets={"a": "box-a", "b": "box-b"})
    try:
        rec = await bridge.start_pipeline(_plan("a", "b"), inputs={}, bundle_ref="demo@0.1.0")
        assert rec.step_state["a"].status == "running"
        assert rec.step_state["b"].status == "running"
        assert await _wait_for(lambda: _submitted_both(engine))
        engine.release.set()
        assert await _wait_for(lambda: _pipeline_done(bridge, rec.pipeline_id))
        assert (await bridge.store.get(rec.pipeline_id)).status == "done"
    finally:
        bridge.stop()


# ── the pieces ─────────────────────────────────────────────────────────────


async def test_target_cap_reads_the_field_then_the_config_key(monkeypatch):
    _connections(monkeypatch,
                 _compute("field", max_in_flight=3),
                 _compute("cfg", config={"max_in_flight": "2"}),
                 _compute("both", max_in_flight=4, config={"max_in_flight": 9}),
                 _compute("unset"),
                 _compute("zero", max_in_flight=0),
                 _compute("junk", config={"max_in_flight": "lots"}))
    assert await target_cap("field") == 3
    assert await target_cap("cfg") == 2, "`--set max_in_flight=2` must work with no CLI change"
    assert await target_cap("both") == 4, "the field wins over the config key"
    assert await target_cap("unset") is None
    assert await target_cap("zero") is None, "<=0 means unset, never 'block everything'"
    assert await target_cap("junk") is None, "a junk cap degrades to unlimited"
    assert await target_cap("nonexistent") is None
    assert await target_cap("") is None


async def test_in_flight_count_holds_only_slot_holding_statuses(monkeypatch):
    """A queued leg holds NO slot (counting it would make the cap self-blocking),
    and a terminal one holds none either.

    The zombie property (ADR-0086 H1, #1239) falls out of this with no extra
    machinery: a leg whose worker died keeps its slot only while its run is still
    `running`, and the orphan reconciler's terminal write drops the count — as
    the last two assertions show, that is the SAME code path as a clean finish.
    """
    store = build_storage().runs

    async def _mk(rid: str, status: str, target: str) -> Run:
        run = Run(id=rid, trigger=TriggerEvent(source="manual"), status=status,
                  compute_target=target)
        await store.create(run)
        return run

    await _mk("r-run", "running", TARGET)
    await _mk("r-queued", "queued", TARGET)
    await _mk("r-other", "running", "other-box")
    await _mk("r-none", "running", "")
    zombie = await _mk("r-zombie", "running", TARGET)

    assert await in_flight_on_target(store, TARGET) == 2
    assert await in_flight_on_target(store, TARGET, exclude_run_id="r-zombie") == 1
    assert await in_flight_on_target(store, "other-box") == 1
    assert await in_flight_on_target(store, "") == 0

    # H1's reconciler terminalizes the zombie → its slot frees itself.
    await store.update(zombie.id, status="failed")
    assert await in_flight_on_target(store, TARGET) == 1


async def test_leg_target_reads_the_kind_s_backend_selection(monkeypatch):
    """`_leg_target` is the same selection `resolve_engine` reads — the target
    name is what the cap is counted by."""
    _stub_flow_resolution(monkeypatch)
    monkeypatch.setenv("OBS_DATAFLOW_TARGET", "data-box")
    monkeypatch.setenv("OBS_ML_TARGET", "gpu-box")
    bridge = PipelineBridge(object(), store=InMemoryPipelineStore())
    step = PipelinePlanStep(id="a", ref="flow:demo/a")

    monkeypatch.setattr(PipelineBridge, "_leg_flow_kind", staticmethod(lambda s: "data"))
    assert bridge._leg_target(step) == "data-box"
    monkeypatch.setattr(PipelineBridge, "_leg_flow_kind", staticmethod(lambda s: "ml"))
    assert bridge._leg_target(step) == "gpu-box"
