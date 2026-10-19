"""dataflow/run operator (#1048): registration, submit/poll round-trip against
the stub engine, control-plane artifact registration, mock path."""
from __future__ import annotations

import asyncio

from cairn.core.dataflow.conformance import StubEngine
from cairn.core.operator import OperationContext, default_registry, load_all_operators
from cairn.core.operator.jobs import is_remote_job_operator
from cairn.core.storage.artifacts import SqliteArtifactStore

from operator_dataflow.operators import run as run_mod
from operator_dataflow.operators.run import DataflowRunOp
from operator_dataflow.schemas.run import DataflowRunInput


def _ctx() -> OperationContext:
    return OperationContext(pipeline_id="run-df-1", node_name="run-dataflow")


def _inp(**kw) -> DataflowRunInput:
    return DataflowRunInput(stages=[{"stage": "noop"}, {"stage": "noop"}], **kw)


def _run(coro):
    return asyncio.run(coro)


def test_registered_and_is_remote_job():
    load_all_operators()
    assert "dataflow/run" in default_registry.list_names()
    assert is_remote_job_operator(DataflowRunOp) is True


def test_mock_needs_no_engine():
    out = DataflowRunOp().mock(_inp(), _ctx())
    assert out.engine == "mock" and out.stats["stages_total"] == 2


def test_submit_poll_registers_artifacts(monkeypatch):
    engine = StubEngine()
    monkeypatch.setattr(run_mod, "resolve_engine", lambda ctx: engine)
    store = SqliteArtifactStore()
    run_mod._reset_artifact_store(store)
    try:
        async def go():
            op = DataflowRunOp()
            inp = _inp()
            handle = await op.submit(inp, _ctx())
            assert handle.backend == "dataflow" and handle.meta["engine"] == "stub"
            status = await op.poll(handle, inp, _ctx())
            assert status.state == "succeeded"
            out = status.output
            assert out["stats"]["stages_run"] == 2
            assert len(out["artifact_ids"]) == 2
            # control-plane registration actually landed, with run lineage
            rec = await store.get(out["artifact_ids"][0])
            assert rec is not None and rec.producer_run_id == "run-df-1"

            # #1050 slice 3: a CTX-LESS poll (the in-process CLI driver path that
            # lost pipeline_id — seen live as empty producer_run_id) still
            # registers with the run id STASHED in the handle at submit.
            handle2 = await op.submit(inp, _ctx())
            bare = OperationContext(pipeline_id="", node_name="poll")
            status2 = await op.poll(handle2, inp, bare)
            assert status2.state == "succeeded"
            rec2 = await store.get(status2.output["artifact_ids"][0])
            assert rec2 is not None and rec2.producer_run_id == "run-df-1"
        _run(go())
    finally:
        run_mod._reset_artifact_store(None)


def test_parent_run_defaults_to_submitting_run(monkeypatch):
    captured = {}

    class _SpyEngine(StubEngine):
        async def submit(self, spec, ctx):
            captured["parent_run"] = spec.parent_run
            return await super().submit(spec, ctx)

    monkeypatch.setattr(run_mod, "resolve_engine", lambda ctx: _SpyEngine())
    _run(DataflowRunOp().submit(_inp(), _ctx()))
    assert captured["parent_run"] == "run-df-1"  # lineage default (#989)
    _run(DataflowRunOp().submit(_inp(parent_run="check-42"), _ctx()))
    assert captured["parent_run"] == "check-42"  # explicit --from wins
