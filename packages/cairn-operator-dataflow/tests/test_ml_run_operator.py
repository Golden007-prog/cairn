"""ml/run operator (ADR-0082 D7, #1074): registration, kind-matched flow_ref
resolution, submit/poll round-trip against the stub engine (engines are
kind-blind), control-plane artifact registration, mock path."""
from __future__ import annotations

import asyncio
import textwrap

import pytest

from cairn.core.dataflow.conformance import StubEngine
from cairn.core.dataflow.defs import DataflowDefError
from cairn.core.operator import OperationContext, default_registry, load_all_operators
from cairn.core.operator.jobs import is_remote_job_operator
from cairn.core.pack import registry as pack_registry
from cairn.core.pack.protocol import Pack
from cairn.core.storage.artifacts import SqliteArtifactStore

from operator_dataflow.operators import run as run_mod
from operator_dataflow.operators import run_ml as run_ml_mod
from operator_dataflow.operators.run_ml import MlRunOp
from operator_dataflow.schemas.run_ml import MlRunInput


def _ctx() -> OperationContext:
    return OperationContext(pipeline_id="run-ml-1", node_name="run-ml")


def _inp(**kw) -> MlRunInput:
    return MlRunInput(stages=[{"stage": "noop"}, {"stage": "noop"}], **kw)


@pytest.fixture()
def ml_flow_pack(tmp_path):
    fdir = tmp_path / "flows"
    fdir.mkdir()
    (fdir / "tune.yaml").write_text(textwrap.dedent("""
        name: tune
        kind: ml
        stages:
          - stage: noop
            config:
              dataset_uri: $config.dataset_uri
            resources:
              gpu: 1
    """))
    (fdir / "wash.yaml").write_text("name: wash\nkind: data\nstages:\n  - stage: noop\n")
    pack = Pack(name="tmp-mlrunpack", version="0.0.1", description="test",
                flows_dir=str(fdir))
    pack_registry.register_pack(pack)
    yield
    pack_registry._PACKS.pop("tmp-mlrunpack", None)


def test_registered_and_is_remote_job():
    load_all_operators()
    assert "ml/run" in default_registry.list_names()
    assert is_remote_job_operator(MlRunOp) is True


def test_mock_needs_no_engine():
    out = MlRunOp().mock(_inp(), _ctx())
    assert out.engine == "mock" and out.stats["stages_total"] == 2


def test_submit_poll_registers_artifacts(monkeypatch):
    engine = StubEngine()
    monkeypatch.setattr(run_ml_mod, "resolve_engine", lambda ctx, kind="ml": engine)
    store = SqliteArtifactStore()
    run_mod._reset_artifact_store(store)
    try:
        async def go():
            op = MlRunOp()
            inp = _inp()
            handle = await op.submit(inp, _ctx())
            assert handle.backend == "mlflow-run" and handle.meta["engine"] == "stub"
            assert handle.meta["run_id"] == "run-ml-1"  # #1050 stash pattern
            status = await op.poll(handle, inp, _ctx())
            assert status.state == "succeeded"
            out = status.output
            assert len(out["artifact_ids"]) == 2
            rec = await store.get(out["artifact_ids"][0])
            assert rec is not None and rec.producer_run_id == "run-ml-1"
        asyncio.run(go())
    finally:
        run_mod._reset_artifact_store(None)


def test_flow_ref_resolves_kind_ml_control_side(monkeypatch, ml_flow_pack):
    captured = {}

    class _SpyEngine(StubEngine):
        async def submit(self, spec, ctx):
            captured["stages"] = spec.stages
            return await super().submit(spec, ctx)

    monkeypatch.setattr(run_ml_mod, "resolve_engine", lambda ctx, kind="ml": _SpyEngine())

    async def go():
        await MlRunOp().submit(
            MlRunInput(flow_ref="tmp-mlrunpack/tune", config={"dataset_uri": "hf://d"}),
            _ctx(),
        )
    asyncio.run(go())
    # engine received CONCRETE stages (engines never read packs), with the
    # $config substitution applied and the resources hint preserved
    assert captured["stages"][0].stage == "noop"
    assert captured["stages"][0].config == {"dataset_uri": "hf://d"}
    assert captured["stages"][0].resources.gpu == 1


def test_flow_ref_kind_mismatch_is_an_error(monkeypatch, ml_flow_pack):
    monkeypatch.setattr(run_ml_mod, "resolve_engine", lambda ctx, kind="ml": StubEngine())

    async def go():
        await MlRunOp().submit(MlRunInput(flow_ref="tmp-mlrunpack/wash"), _ctx())
    with pytest.raises(DataflowDefError, match="kind: data"):
        asyncio.run(go())
