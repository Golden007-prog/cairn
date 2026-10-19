"""#1248 H6d — remote checkpoint PULL through the engine seam.

A remote leg's workdir doesn't exist on the control plane, so the bridge's
relay tick asks the ENGINE to pull ``checkpoints/`` into a local staging dir
(`fetch_checkpoints(handle, dest_dir)`), then relays the staging dir through
the SAME ``relay_local_checkpoints`` path a local workdir uses. These tests
pin that seam hermetically (a stub engine playing the box); the live SSH
half is the H6d box acceptance.

Harness shape follows test_pipeline_flow_leg_async.py: real dispatcher, bus
and bridge wiring; stub engine + stubbed flow resolution.
"""
from __future__ import annotations

import asyncio
import json
import os
import types
from pathlib import Path

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.dataflow.contract import StageRef
from cairn.core.dispatch.dispatcher import RunDispatcher
from cairn.core.dispatch.pipeline_bridge import PipelineBridge
from cairn.core.engine.bundle_pipeline import PipelinePlan, PipelinePlanStep
from cairn.core.storage import build_storage
from cairn.core.storage.artifacts import SqliteArtifactStore
from cairn.core.storage.pipeline_store import InMemoryPipelineStore
from tests.core.test_trigger_server_dispatch import FakeRunner


def _stub_flow_resolution(monkeypatch):
    stage = StageRef(stage="clean.run", checkpoint=True)
    monkeypatch.setattr("cairn.core.dataflow.defs.load_flow_defs", lambda: {})
    monkeypatch.setattr(
        "cairn.core.dataflow.defs.resolve_flow_ref",
        lambda ref, cfg, kind="data": [stage],
    )
    monkeypatch.setattr("cairn.core.dispatch.leg_executor._POLL_INTERVAL_S", 0.01)


def _fake_checkpoint(dest_dir: Path, *, index: int = 0, stage: str = "clean.run",
                     spec_key: str = "k" * 16) -> None:
    """What a completed engine-side pull leaves in the staging dir."""
    d = dest_dir / "checkpoints" / f"stage-{index:02d}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "current.jsonl").write_text('{"row": 1}\n', encoding="utf-8")
    (d / "manifest.json").write_text(json.dumps({
        "spec_key": spec_key, "stage_index": index, "stage": stage,
        "files": [{"name": "current.jsonl", "content_hash": "sha256:aa", "size": 11}],
        "stats": {"rows": 1},
    }), encoding="utf-8")


class _RemoteEngine:
    """A box-shaped engine: the handle meta has NO local workdir, and
    checkpoints only exist through fetch_checkpoints."""

    name = "stub-remote"

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.fetch_calls = 0
        self.has_checkpoint = False

    def validate(self, spec):
        return []

    async def submit(self, spec, ctx):
        return types.SimpleNamespace(
            job_id="rj1", engine=self.name,
            meta={"job_handle": {"job_id": "rj1"}},   # remote shape: no workdir
        )

    async def poll(self, handle):
        state = "succeeded" if self.release.is_set() else "running"
        return types.SimpleNamespace(state=state, metrics={}, error="")

    async def cancel(self, handle):
        self.release.set()

    async def result(self, handle):
        return types.SimpleNamespace(stats={"clean.output": "box:/out"})

    async def fetch_checkpoints(self, handle, dest_dir: Path) -> bool:
        assert handle.job_id == "rj1" and handle.engine == self.name
        self.fetch_calls += 1
        if not self.has_checkpoint:
            return False
        _fake_checkpoint(Path(dest_dir))
        return True


@pytest.fixture()
async def store(tmp_path):
    s = SqliteArtifactStore(path=str(tmp_path / "artifacts.db"))
    await s.ensure_schema()
    yield s
    await s.close()


async def _wait(bridge, pid, *, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        rec = await bridge.store.get(pid)
        if rec and rec.status in ("done", "failed", "cancelled"):
            return rec
        await asyncio.sleep(0.02)
    return await bridge.store.get(pid)


async def test_remote_checkpoint_pull_relays_and_audits(monkeypatch, store, tmp_path):
    """The full seam: beat → engine pull → relay → registry + audit."""
    monkeypatch.setenv("CAIRN_CHECKPOINT_DIR", str(tmp_path / "durable"))
    # the relay reads the env at call time via _DEFAULT_ROOT — re-pin it
    import cairn.core.dataflow.checkpoints as ck
    monkeypatch.setattr(ck, "_DEFAULT_ROOT", tmp_path / "durable")

    _stub_flow_resolution(monkeypatch)
    engine = _RemoteEngine()
    engine.has_checkpoint = True
    monkeypatch.setattr(PipelineBridge, "_leg_engine", lambda self, s: engine)

    audits: list[tuple[str, dict]] = []

    async def _capture(self, rec, step_id, event, detail):
        audits.append((event, dict(detail)))

    monkeypatch.setattr(PipelineBridge, "_audit", _capture)

    d = RunDispatcher(FakeRunner(), backend=build_storage())
    bridge = PipelineBridge(d, store=InMemoryPipelineStore(), artifact_store=store)
    await bridge.start()
    try:
        plan = PipelinePlan(name="pl", steps=[
            PipelinePlanStep(id="clean", ref="flow:demo/clean"),
        ])
        rec = await bridge.start_pipeline(plan, inputs={}, bundle_ref="demo@0.1.0")
        engine.release.set()
        rec = await _wait(bridge, rec.pipeline_id)
        assert rec.status == "done", f"pipeline did not finish: {rec.status}"

        # the pull ran and the checkpoint is REGISTERED + durable
        assert engine.fetch_calls >= 1
        recs = await store.list_artifacts(kind="dataset")
        cks = [r for r in recs if dict(r.stats).get("checkpoint")]
        assert len(cks) == 1
        assert dict(cks[0].stats)["stage"] == "clean.run"
        dest = Path(str(cks[0].uri).removeprefix("file://"))
        assert (dest / "current.jsonl").is_file()

        # audited — one checkpoint_registered with the relayed stage + uri
        regs = [d for ev, d in audits if ev == "checkpoint_registered"]
        assert len(regs) == 1 and regs[0]["stage"] == "clean.run"
        assert regs[0]["uri"].startswith("file://")
    finally:
        bridge.stop()


async def test_no_pull_before_submit_and_engines_without_the_seam(monkeypatch, store):
    """Guards: an empty handle (pre-submit) never pulls; an engine without
    fetch_checkpoints (local/metaflow shape) silently no-ops."""
    _stub_flow_resolution(monkeypatch)
    d = RunDispatcher(FakeRunner(), backend=build_storage())
    bridge = PipelineBridge(d, store=InMemoryPipelineStore(), artifact_store=store)

    # #1429 — the seam lives in the shared FlowLegHooks now (both owners)
    from cairn.core.dispatch.leg_hooks import FlowLegHooks

    async def _emit(*a, **k):
        pass
    plain = types.SimpleNamespace(name="plain")
    hooks = FlowLegHooks(pipeline_id="p1", step_id="clean", leg_ref="flow:demo/clean",
                         run_id="r-pre", pipeline_store=bridge.store, run_store=d.store,
                         emit=_emit, artifact_store=store, engine=plain)
    # pre-submit: no job_id yet → no engine call at all
    assert await hooks._pull_remote_checkpoints() is None

    # engine without the seam → None (relay becomes a no-op for remote legs)
    hooks.leg_handle = {"job_id": "j1", "engine": "plain", "meta": {}}
    assert await hooks._pull_remote_checkpoints() is None
    assert await store.list_artifacts(kind="dataset") == []
    # relay_checkpoints end-to-end no-op (no exception, nothing registered)
    await hooks.relay_checkpoints()
    assert await store.list_artifacts(kind="dataset") == []


async def test_pull_returning_false_relays_nothing(monkeypatch, store):
    """An engine that finds no checkpoints yet (False) must not relay."""
    _stub_flow_resolution(monkeypatch)
    engine = _RemoteEngine()          # has_checkpoint stays False
    monkeypatch.setattr(PipelineBridge, "_leg_engine", lambda self, s: engine)
    d = RunDispatcher(FakeRunner(), backend=build_storage())
    bridge = PipelineBridge(d, store=InMemoryPipelineStore(), artifact_store=store)

    from cairn.core.dispatch.leg_hooks import FlowLegHooks

    async def _emit(*a, **k):
        pass
    hooks = FlowLegHooks(pipeline_id="p2", step_id="clean", leg_ref="flow:demo/clean",
                         run_id="r-none", pipeline_store=bridge.store, run_store=d.store,
                         emit=_emit, artifact_store=store, engine=engine)
    hooks.leg_handle = {"job_id": "rj1", "engine": engine.name, "meta": {}}
    await hooks.relay_checkpoints()
    assert engine.fetch_calls == 1
    assert await store.list_artifacts(kind="dataset") == []
