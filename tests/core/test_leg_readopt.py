"""#1429 — adopt-by-handle (ADR-0086 H1) and the ONE side-channel assembly both
pipeline owners use (``FlowLegHooks``).

Before: the bridge hand-built persist/heartbeat/relay closures inline and the
Temporal owner built none; after a restart NEITHER owner re-polled a persisted
handle — the leg orphaned and the reconciler failed it. Now ``execute_leg(adopt=)``
re-attaches (poll → result → register) without submitting, and a bridge restart
acts on ``plan_resume().reattach``.
"""
from __future__ import annotations

import asyncio
import os
import types

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.dataflow.contract import StageRef
from cairn.core.dispatch.coordination import CriticalSections
from cairn.core.dispatch.leg_executor import execute_leg
from cairn.core.dispatch.leg_hooks import FlowLegHooks
from cairn.core.engine.bundle_pipeline import PipelinePlan, PipelinePlanStep
from cairn.core.storage.pipeline_store import InMemoryPipelineStore, PipelineRecord, StepState


def _stub_resolution(monkeypatch, *, kind="data"):
    stage = StageRef(stage="clean.run")
    monkeypatch.setattr("cairn.core.dataflow.defs.load_flow_defs", lambda: {
        "demo/clean": types.SimpleNamespace(kind=kind),
    })
    monkeypatch.setattr(
        "cairn.core.dataflow.defs.resolve_flow_ref",
        lambda ref, cfg, kind="data": [stage],
    )
    monkeypatch.setattr("cairn.core.dispatch.leg_executor._POLL_INTERVAL_S", 0.01)


class _Engine:
    """Records submits and answers polls from a script keyed by job_id."""

    name = "stub"

    def __init__(self, script: dict[str, list[str]]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.submits = 0
        self.polled: list[str] = []

    def validate(self, spec):
        return []

    async def submit(self, spec, ctx):
        self.submits += 1
        return types.SimpleNamespace(job_id=f"new{self.submits}", engine=self.name, meta={"w": "x"})

    async def poll(self, handle):
        self.polled.append(handle.job_id)
        seq = self.script.get(handle.job_id) or ["succeeded"]
        state = seq.pop(0) if len(seq) > 1 else seq[0]
        if state == "failed":
            return types.SimpleNamespace(state="failed", error="proc gone", metrics={}, retriable=True)
        return types.SimpleNamespace(state=state, metrics={}, error="", retriable=False)

    async def cancel(self, handle):
        pass

    async def result(self, handle):
        return types.SimpleNamespace(stats={"clean.output": f"s3://{handle.job_id}"}, output_artifacts=[])


# ── execute_leg(adopt=) ─────────────────────────────────────────────────────

async def test_adopt_watches_the_persisted_job_and_never_submits(monkeypatch):
    _stub_resolution(monkeypatch)
    eng = _Engine({"old-7": ["running", "succeeded"]})
    adopted: list[dict] = []

    async def _on_adopt(h):
        adopted.append(h)

    out = await execute_leg(
        "flow:demo/clean", inputs={}, engine=eng, run_id="r1",
        adopt={"job_id": "old-7", "engine": "stub", "meta": {"workdir": "/tmp/w"}},
        on_adopt=_on_adopt,
    )
    assert out.ok and out.stats["clean.output"] == "s3://old-7"
    assert eng.submits == 0                          # RE-ATTACHED, not resubmitted
    assert eng.polled[:1] == ["old-7"]
    assert adopted == [{"job_id": "old-7", "engine": "stub", "meta": {"workdir": "/tmp/w"}}]
    assert out.handle.job_id == "old-7" and out.handle.meta == {"workdir": "/tmp/w"}


async def test_adopt_skips_budget_reservation(monkeypatch):
    """The spend was admitted at the ORIGINAL submit; re-attaching is not new
    spend — a series at its cap must still be able to watch its own job."""
    _stub_resolution(monkeypatch)
    from cairn.core import budget_reservations as B
    # #1530 — pin the in-memory backend; `OBS_STORAGE=postgres://…` in the shell
    # would otherwise route this hermetic assertion at a live database.
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    B.reset_reservations()
    eng = _Engine({"old": ["succeeded"]})
    out = await execute_leg(
        "flow:demo/clean", inputs={}, engine=eng, run_id="r1",
        series_id="s1", estimated_usd=50.0, spent_usd=100.0, budget_usd=100.0,   # ZERO headroom
        adopt={"job_id": "old", "engine": "stub", "meta": {}},
    )
    assert out.ok and eng.submits == 0
    # …while a FRESH leg on the same series is refused, as before
    out2 = await execute_leg(
        "flow:demo/clean", inputs={}, engine=eng, run_id="r2",
        series_id="s1", estimated_usd=50.0, spent_usd=100.0, budget_usd=100.0)
    assert not out2.ok and "exceed" in out2.error


async def test_adopted_job_failure_falls_through_to_a_fresh_resubmit(monkeypatch):
    """The dead-process case: the adopted job reports failed+retriable (its
    process is gone), so the H2 retry submits ONE fresh attempt — seeded by
    the resume provider — and that succeeds."""
    _stub_resolution(monkeypatch)
    eng = _Engine({"old": ["failed"], "new1": ["succeeded"]})
    asked: list[str] = []

    async def _resume(key):
        asked.append(key)
        return None

    out = await execute_leg(
        "flow:demo/clean", inputs={}, engine=eng, run_id="r1",
        adopt={"job_id": "old", "engine": "stub", "meta": {}},
        resume_provider=_resume,
    )
    assert out.ok and out.stats["clean.output"] == "s3://new1"
    assert eng.submits == 1 and eng.polled[0] == "old"
    assert asked, "the resubmit attempt consulted the checkpoint registry"


async def test_empty_adopt_is_a_normal_submit(monkeypatch):
    _stub_resolution(monkeypatch)
    eng = _Engine({})
    out = await execute_leg("flow:demo/clean", inputs={}, engine=eng, run_id="r1", adopt={})
    assert out.ok and eng.submits == 1


# ── FlowLegHooks: the shared assembly ───────────────────────────────────────

class _RunStore:
    def __init__(self):
        self.updates: list[dict] = []

    async def update(self, run_id, **kw):
        self.updates.append({"run_id": run_id, **kw})


async def _hooks(pstore, emits):
    async def _emit(run_id, event, data):
        emits.append((run_id, event, data))
    return FlowLegHooks(
        pipeline_id="pl1", step_id="prep", leg_ref="flow:demo/clean", run_id="run-1",
        pipeline_store=pstore, run_store=_RunStore(), emit=_emit, artifact_store=None,
    )


async def test_on_submit_persists_handle_and_run_id_on_the_step():
    pstore = InMemoryPipelineStore()
    await pstore.put(PipelineRecord(pipeline_id="pl1", status="running",
                                    step_state={"prep": StepState(status="running")}))
    emits: list = []
    h = await _hooks(pstore, emits)
    await h.on_submit(types.SimpleNamespace(job_id="mf-42", engine="metaflow", meta={"pgid": 42}))
    st = (await pstore.get("pl1")).step_state["prep"]
    assert st.handle == {"job_id": "mf-42", "engine": "metaflow", "meta": {"pgid": 42}}
    assert st.run_id == "run-1" and st.started_at
    # a NEW owner process adopts exactly this
    h2 = await _hooks(pstore, emits)
    assert await h2.persisted_handle() == st.handle
    # …but not once the step is terminal
    st.status = "done"
    rec = await pstore.get("pl1"); rec.step_state["prep"] = st; await pstore.put(rec)
    assert await h2.persisted_handle() is None


async def test_execute_kwargs_is_the_full_side_channel_set():
    pstore = InMemoryPipelineStore()
    h = await _hooks(pstore, [])
    kw = h.execute_kwargs(retry="R", adopt={"job_id": "j"})
    assert set(kw) == {"on_submit", "heartbeat", "retry", "on_retry", "resume_provider",
                       "on_resume", "artifact_store", "adopt", "on_adopt"}
    assert kw["retry"] == "R" and kw["adopt"] == {"job_id": "j"}
    import inspect
    from cairn.core.dispatch.leg_executor import execute_leg as ex
    assert set(kw) <= set(inspect.signature(ex).parameters), "every hook is a real execute_leg kwarg"


async def test_retry_resume_and_adopt_are_visible_run_events():
    pstore = InMemoryPipelineStore()
    emits: list = []
    h = await _hooks(pstore, emits)
    await h.on_retry(2, "boom")
    await h.on_resume(types.SimpleNamespace(stage_index=3, spec_key="k"))
    await h.on_adopt({"job_id": "mf-1", "engine": "metaflow"})
    kinds = [e[1] for e in emits]
    assert kinds == ["leg_retrying", "leg_resumed_from_checkpoint", "leg_readopted"]
    assert emits[0][2]["attempt"] == 2 and emits[2][2]["job_id"] == "mf-1"


async def test_heartbeat_stamps_liveness():
    pstore = InMemoryPipelineStore()
    h = await _hooks(pstore, [])
    await h.heartbeat()
    assert h.run_store.updates and "last_heartbeat_at" in h.run_store.updates[0]


# ── the bridge on restart: plan_resume is now on the run path ───────────────

async def test_bridge_restart_readopts_running_flow_leg_by_handle(monkeypatch):
    """A pipeline whose flow leg was RUNNING (with a persisted handle) when the
    process died: a fresh bridge re-attaches to it — no orphan, no resubmit."""
    from cairn.core.dispatch import pipeline_bridge as PB
    from cairn.core.dispatch.pipeline_bridge import PipelineBridge

    pstore = InMemoryPipelineStore()
    rec = PipelineRecord(
        pipeline_id="pl-restart", status="running",
        plan=PipelinePlan(name="pl", steps=[PipelinePlanStep(id="prep", ref="flow:demo/clean")]),
        step_state={"prep": StepState(status="running", run_id="run-prep",
                                      handle={"job_id": "mf-9", "engine": "stub", "meta": {}})},
    )
    await pstore.put(rec)

    run = types.SimpleNamespace(id="run-prep", status="running", inputs={"a": 1})

    class _RS:
        async def get(self, rid):
            return run if rid == "run-prep" else None
        async def update(self, *a, **k):
            pass

    calls: list[dict] = []

    async def _fake_run_flow_leg(self, rec_, s, run_, resolved, *, adopt=None):
        calls.append({"step": s.id, "run": run_.id, "adopt": adopt, "resolved": resolved})

    monkeypatch.setattr(PipelineBridge, "_run_flow_leg", _fake_run_flow_leg)
    bridge = PipelineBridge.__new__(PipelineBridge)
    bridge.store = pstore
    bridge.dispatcher = types.SimpleNamespace(store=_RS())
    bridge._run_to_pipeline = {}
    bridge._leg_tasks = {}
    bridge._sections = CriticalSections(namespace="pipeline")  # ADR-0125: adopt holds it
    monkeypatch.setattr(PB, "leg_kind_of", lambda ref: "flow")

    await bridge._adopt_in_flight()
    await asyncio.sleep(0)   # let the created task run
    assert bridge._run_to_pipeline == {"run-prep": "pl-restart"}
    assert calls == [{"step": "prep", "run": "run-prep",
                      "adopt": {"job_id": "mf-9", "engine": "stub", "meta": {}},
                      "resolved": {"a": 1}}]


async def test_bridge_restart_fails_and_parks_a_handleless_running_leg(monkeypatch):
    """No handle = nothing to re-attach to (plan_resume: orphaned), and the H1
    reconciler judges only a heartbeat-stamped run. #2555: the dead run is
    settled `failed` and the step waits for a person — never a blind resubmit."""
    from cairn.core.dispatch import pipeline_bridge as PB
    from cairn.core.dispatch.pipeline_bridge import PipelineBridge

    pstore = InMemoryPipelineStore()
    rec = PipelineRecord(pipeline_id="pl-o", status="running",
                         plan=PipelinePlan(name="pl", steps=[PipelinePlanStep(id="prep", ref="flow:demo/clean")]),
                         step_state={"prep": StepState(status="running", run_id="run-o")})
    await pstore.put(rec)
    calls: list = []

    async def _fake(self, *a, **k):
        calls.append(1)
    monkeypatch.setattr(PipelineBridge, "_run_flow_leg", _fake)
    monkeypatch.setattr(PB, "leg_kind_of", lambda ref: "flow")
    bridge = PipelineBridge.__new__(PipelineBridge)
    bridge.store = pstore
    updates: dict = {}

    async def _get(rid):
        return types.SimpleNamespace(id=rid, status="running")

    async def _update(rid, **fields):
        updates[rid] = fields
    bridge.dispatcher = types.SimpleNamespace(store=types.SimpleNamespace(get=_get, update=_update))
    bridge._run_to_pipeline = {}
    bridge._leg_tasks = {}
    bridge._sections = CriticalSections(namespace="pipeline")  # ADR-0125: adopt holds it
    await bridge._adopt_in_flight()
    assert calls == [] and bridge._run_to_pipeline == {}
    assert updates["run-o"]["status"] == "failed"
    assert (await pstore.get("pl-o")).step_state["prep"].status == "awaiting_advance"


@pytest.mark.parametrize("status", ["done", "failed"])
async def test_bridge_restart_ignores_terminal_pipelines(status):
    from cairn.core.dispatch.pipeline_resume import plan_resume
    rec = PipelineRecord(pipeline_id="x", status=status,
                         step_state={"a": StepState(status="running", handle={"job_id": "j"})})
    assert plan_resume(rec).is_noop


@pytest.mark.parametrize("sectioned", [True, False])
async def test_a_concurrent_advance_cannot_erase_the_persisted_handle(sectioned):
    """WP7 crash cell on the Postgres bus: the handle write is a read-modify-write of the
    whole record. An advance that read the record before the write (a SQL store returns a
    fresh copy, hence ``deep=True``) and puts it back afterwards erased the re-attach token
    while the leg ran. Inside the owner's section the write waits for that advance."""
    pstore = InMemoryPipelineStore()
    await pstore.put(PipelineRecord(pipeline_id="pl1", status="running",
                                    step_state={"prep": StepState(status="running")}))
    sections = CriticalSections(namespace="pipeline")

    async def _emit(run_id, event, data):
        return None
    hooks = FlowLegHooks(
        pipeline_id="pl1", step_id="prep", leg_ref="flow:demo/clean", run_id="run-1",
        pipeline_store=pstore, run_store=_RunStore(), emit=_emit, artifact_store=None,
        section=sections.hold if sectioned else None,
    )
    async with sections.hold("pl1"):                      # an advance in its section
        stale = (await pstore.get("pl1")).model_copy(deep=True)
        submit = asyncio.create_task(hooks.on_submit(types.SimpleNamespace(job_id="b-1", engine="builtin", meta={})))
        for _ in range(5):
            await asyncio.sleep(0)                        # the leg's submit returns meanwhile
        await pstore.put(stale)                           # …and the advance writes back what it read
    await submit
    handle = (await pstore.get("pl1")).step_state["prep"].handle
    if sectioned:
        assert handle and handle["job_id"] == "b-1"
    else:
        assert not handle                                 # the race this fix closes
