"""ADR-0118 (#2242 V1-A) — stage contracts in the interpreter and at the dataflow/run door.

A test-only kernel (``probe``) opts in to ``contract://probe.run/1.0.0``; it is injected into
the runner's registry, never shipped. The admitted stage contracts are built exactly as the
launch door's reader bundles them (self-contained schemas) and stamped by the shared
``stamp``. Every check has a rejecting case:

- runner: identity drift, a configuration violation before the kernel, a raw-output
  violation that leaves no marker, a resume with a drifted or missing ``kernel_version``,
  a mapped stage's per-item configuration and merged output, a stamped spec without an
  envelope; a legacy stage is byte-identical;
- dataflow/run and ml/run (one door, each case run on both): submit stamps the spec,
  refuses an undeclared engine, an oversized spec, a configuration violation and an
  opted-in stage without its entry; poll re-checks descriptors before registration.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from cairn.core.dataflow import DataflowSpec, StageMap, StageRef
from cairn.core.dataflow.conformance import StubEngine
from cairn.core.dataflow.conformance_check import StageContractError, bundle, stamp
from cairn.core.operator import OperationContext
from cairn.core.storage.artifacts import SqliteArtifactStore

import cairn.core.dataflow.runner as runner_mod
from cairn.core.dataflow.runner import _marker, run_dataflow
from operator_dataflow.operators import run as run_mod
from operator_dataflow.operators import run_ml as run_ml_mod
from operator_dataflow.operators.run import DataflowRunOp
from operator_dataflow.operators.run_ml import MlRunOp
from operator_dataflow.schemas.run import DataflowRunInput
from operator_dataflow.schemas.run_ml import MlRunInput

_STAGE = "contract://probe.run/1.0.0"
_CFG, _OUT = "contract://probe.configuration/1.0.0", "contract://probe.output/1.0.0"
_SCHEMAS = {_CFG: {"type": "object", "properties": {"rows": {"type": "integer"}, "item": {"type": "integer"}}},
            _OUT: {"type": "object", "required": ["rows"], "properties": {"rows": {"$ref": "contract://probe.count/1.0.0"}}},
            "contract://probe.count/1.0.0": {"type": "integer", "minimum": 0}}
CALLS: list = []


def probe(workdir, config, inputs):
    """The opted-in test kernel: reports ``rows`` straight from its config."""
    CALLS.append(dict(config))
    return {"rows": config.get("rows", 0)}


probe.implements = (_STAGE,)


def _entry(version="1.2.3"):
    return {"stage": "probe", "contract": {"capability_ref": _STAGE, "version": version, "configuration": _CFG, "output": _OUT},
            "schemas": {_CFG: bundle(_SCHEMAS, _CFG), _OUT: bundle(_SCHEMAS, _OUT)}}


def _carried(index=1, **kw):
    return {"leg": "L", "error": "", "stages": {str(index): _entry(**kw)}}


def _implements(stage):
    return tuple(getattr({"probe": probe}.get(stage), "implements", ()))


def _stamped(*stages, carried=None):
    spec = DataflowSpec(stages=list(stages), parent_run="r1")
    return stamp(spec, carried if carried is not None else _carried(), _implements)


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    CALLS.clear()
    real = runner_mod.load_stages
    monkeypatch.setattr(runner_mod, "load_stages", lambda: {**real(), "probe": probe})
    versions = {"probe": "1.2.3", "noop": "0.9.0"}
    monkeypatch.setattr(runner_mod, "stage_versions", lambda: dict(versions))
    return versions


# ── the interpreter (A10) ────────────────────────────────────────────────────

def test_a_conforming_opted_in_stage_runs(tmp_path):
    res = run_dataflow(_stamped(StageRef(stage="noop"), StageRef(stage="probe", config={"rows": 2})), tmp_path)
    assert res.stats["probe.rows"] == 2 and res.stats["probe.kernel_version"] == "1.2.3"


@pytest.mark.parametrize("drift", ["version", "revision"])
def test_identity_drift_refuses(tmp_path, registry, monkeypatch, drift):
    if drift == "version":
        registry["probe"] = "9.9.9"
    else:
        monkeypatch.setattr(probe, "implements", ("contract://probe.run/2.0.0",))
    with pytest.raises(StageContractError, match="drifted kernel"):
        run_dataflow(_stamped(StageRef(stage="noop"), StageRef(stage="probe", config={"rows": 2})), tmp_path)
    assert CALLS == []


def test_a_configuration_violation_refuses_before_the_kernel(tmp_path):
    with pytest.raises(StageContractError, match="configuration/rows"):
        run_dataflow(_stamped(StageRef(stage="noop"), StageRef(stage="probe", config={"rows": "two"})), tmp_path)
    assert CALLS == []


def test_a_raw_output_violation_refuses_and_leaves_no_marker(tmp_path):
    with pytest.raises(StageContractError, match="output/rows"):
        run_dataflow(_stamped(StageRef(stage="noop"), StageRef(stage="probe", config={"rows": -1})), tmp_path)
    assert CALLS == [{"rows": -1}]
    assert _marker(tmp_path, 0, "noop").exists() and not _marker(tmp_path, 1, "probe").exists()


@pytest.mark.parametrize("frozen", [{"rows": 2, "kernel_version": "0.0.1"}, {"rows": 2}, {"rows": -5, "kernel_version": "1.2.3"}])
def test_a_resume_rechecks_frozen_stats(tmp_path, frozen):
    spec = _stamped(StageRef(stage="noop"), StageRef(stage="probe", config={"rows": 2}))
    _marker(tmp_path, 1, "probe").write_text(json.dumps(frozen), encoding="utf-8")
    with pytest.raises(StageContractError):
        run_dataflow(spec, tmp_path)
    assert CALLS == []


def test_a_resume_with_the_admitted_kernel_version_conforms(tmp_path):
    spec = _stamped(StageRef(stage="noop"), StageRef(stage="probe", config={"rows": 2}))
    run_dataflow(spec, tmp_path)
    CALLS.clear()
    assert run_dataflow(spec, tmp_path).stats["probe.rows"] == 2 and CALLS == []


def _mapped(items, rows=1):
    return StageRef(stage="probe", config={"rows": rows, "items": items},
                    map=StageMap(over="$config.items", **{"as": "item"}))


def test_a_mapped_stage_checks_each_items_configuration(tmp_path):
    with pytest.raises(StageContractError, match="configuration/item"):
        run_dataflow(_stamped(StageRef(stage="noop"), _mapped([1, "two"])), tmp_path)


def test_a_mapped_stage_checks_the_merged_output(tmp_path, monkeypatch):
    """The pool children re-resolve the kernel from the real registry; the parent's merged
    result (``stats_sum``) is what the output contract judges."""
    monkeypatch.setattr(runner_mod, "_map_item", lambda task: probe(None, task[2], task[3]))
    monkeypatch.setattr("cairn.core.operator.batch.map_shards_local",
                        lambda fn, tasks, requested_workers=None: [fn(t) for t in tasks])
    ok = run_dataflow(_stamped(StageRef(stage="noop"), _mapped([1, 2], rows=3)), tmp_path / "ok")
    assert ok.stats["probe.rows"] == 6 and ok.stats["probe.items_total"] == 2
    with pytest.raises(StageContractError, match="output/rows"):
        run_dataflow(_stamped(StageRef(stage="noop"), _mapped([1, 2], rows=-3)), tmp_path / "bad")


def test_a_stamped_spec_without_the_envelope_refuses(tmp_path):
    spec = _stamped(StageRef(stage="noop"), StageRef(stage="probe", config={"rows": 2}))
    bare = spec.model_copy(update={"stages": [spec.stages[0], spec.stages[1].model_copy(update={"contract": None})]})
    with pytest.raises(StageContractError, match="no contract"):
        run_dataflow(bare, tmp_path)
    assert CALLS == []


def test_an_unstamped_spec_runs_an_opted_in_stage_as_legacy(tmp_path):
    """D6 — the dev loop, `cairn flow export`, an unadmitted template."""
    res = run_dataflow(DataflowSpec(stages=[StageRef(stage="probe", config={"rows": -1})]), tmp_path)
    assert res.stats["probe.rows"] == -1


def test_a_legacy_stage_is_byte_identical(tmp_path):
    """Golden: a legacy stage's marker, stats and descriptor do not change, alone or beside
    an opted-in stage in a stamped spec."""
    alone = run_dataflow(DataflowSpec(stages=[StageRef(stage="noop")]), tmp_path / "a")
    version = alone.stats["noop.kernel_version"]
    assert _marker(tmp_path / "a", 0, "noop").read_text() == json.dumps({"rows": 0, "kernel_version": version})
    assert alone.stats == {"stages_total": 1, "stages_done": 1, "noop.rows": 0, "noop.kernel_version": version}
    beside = run_dataflow(_stamped(StageRef(stage="noop"), StageRef(stage="probe", config={"rows": 1})), tmp_path / "b")
    assert _marker(tmp_path / "b", 0, "noop").read_text() == _marker(tmp_path / "a", 0, "noop").read_text()
    assert beside.output_artifacts[0].stats == alone.output_artifacts[0].stats


# ── the dataflow/run and ml/run doors (A9): one stamp, one set of refusals ──

class _Engine(StubEngine):
    carries_stage_contracts = True

    def __init__(self, rows=0):
        super().__init__(known_stages={"noop", "probe"})
        self.rows, self.specs = rows, []

    async def submit(self, spec, ctx):
        self.specs.append(spec)
        return await super().submit(spec, ctx)

    async def result(self, handle):
        res = await super().result(handle)
        for d in res.output_artifacts:
            d.stats = {"rows": self.rows, "kernel_version": "1.2.3"}
        return res


_INPUT = {DataflowRunOp: DataflowRunInput, MlRunOp: MlRunInput}
both_doors = pytest.mark.parametrize("op", list(_INPUT), ids=["dataflow/run", "ml/run"])


def _inp(rows=2, op=DataflowRunOp, **kw):
    return _INPUT[op](stages=[{"stage": "noop"}, {"stage": "probe", "config": {"rows": rows, **kw}}])


def _ctx(carried=None):
    return OperationContext(pipeline_id="run-df-1", node_name="run-scan", dataflow_contracts=carried)


@pytest.fixture
def door(monkeypatch):
    monkeypatch.setattr("cairn.core.dataflow.defs.stage_implements", _implements)
    store = SqliteArtifactStore()
    run_mod._reset_artifact_store(store)
    yield store
    run_mod._reset_artifact_store(None)


def _engine(monkeypatch, **kw):
    engine = _Engine(**kw)
    monkeypatch.setattr(run_mod, "resolve_engine", lambda ctx: engine)
    monkeypatch.setattr(run_ml_mod, "resolve_engine", lambda ctx, kind=None: engine)
    return engine


@both_doors
def test_submit_stamps_the_spec_and_poll_registers_conforming_descriptors(door, monkeypatch, op):
    engine = _engine(monkeypatch)

    async def go():
        handle = await op().submit(_inp(op=op), _ctx(_carried()))
        (spec,) = engine.specs
        assert spec.contracts.leg == "L" and spec.stages[1].contract.capability_ref == _STAGE
        assert spec.stages[0].contract is None and handle.meta["dataflow_contracts"] == _carried()
        assert handle.meta["stages_total"] == 2
        status = await op().poll(handle, _inp(op=op), _ctx())
        assert status.state == "succeeded" and len(status.output["artifact_ids"]) == 2
    asyncio.run(go())


@both_doors
def test_a_non_conforming_descriptor_refuses_before_registration(door, monkeypatch, op):
    _engine(monkeypatch, rows=-1)
    registered = []
    monkeypatch.setattr(door, "register", lambda *a, **k: registered.append(a))

    async def go():
        handle = await op().submit(_inp(op=op), _ctx(_carried()))
        with pytest.raises(StageContractError, match="output/rows"):
            await op().poll(handle, _inp(op=op), _ctx())
    asyncio.run(go())
    assert registered == []


@both_doors
@pytest.mark.parametrize("case", ["undeclared", "oversized", "no_entry", "configuration"])
def test_the_door_refuses_before_the_engine(door, monkeypatch, case, op):
    engine = _engine(monkeypatch)
    inp, carried = _inp(op=op), _carried()
    if case == "undeclared":
        engine.carries_stage_contracts = False
    elif case == "oversized":
        inp = _inp(op=op, pad="x" * (100 * 1024))
    elif case == "no_entry":
        carried = {"leg": "L", "error": "", "stages": {}}
    else:
        inp = _inp(rows="two", op=op)
    match = {"undeclared": "carries_stage_contracts", "oversized": "bound",
             "no_entry": "no contract", "configuration": "configuration/rows"}[case]
    with pytest.raises(StageContractError, match=match):
        asyncio.run(op().submit(inp, _ctx(carried)))
    assert engine.specs == []


@both_doors
def test_outside_an_admitted_leg_the_door_is_legacy(door, monkeypatch, op):
    engine = _engine(monkeypatch)
    handle = asyncio.run(op().submit(_inp(rows=-1, op=op), _ctx(None)))
    assert engine.specs[0].contracts is None and "dataflow_contracts" not in handle.meta


def test_relaunch_keeps_the_contracts_through_the_job_record(door, monkeypatch):
    """The durable control loop rebuilds the context from the job record; the carried
    contracts come back from the handle meta, so a relaunch is stamped too."""
    from cairn.core.operator.job_control import build_default_relaunch
    from cairn.core.operator.job_store import RemoteJobRecord

    engine = _engine(monkeypatch)
    handle = asyncio.run(DataflowRunOp().submit(_inp(), _ctx(_carried())))
    rec = RemoteJobRecord(run_id="run-df-1", node_name="run-scan", provider="dataflow/run", job_id=handle.job_id,
                          backend=handle.backend, job_meta=dict(handle.meta), input=_inp().model_dump())
    registry = type("Reg", (), {"get": staticmethod(lambda name: DataflowRunOp)})()
    asyncio.run(build_default_relaunch(registry)(rec))
    assert [s.contracts.leg if s.contracts else None for s in engine.specs] == ["L", "L"]


def test_a_stamped_spec_round_trips_through_the_env_json():
    """A stamped spec round-trips through the env JSON the engines use."""
    spec = _stamped(StageRef(stage="noop"), StageRef(stage="probe", config={"rows": 2}))
    assert DataflowSpec.model_validate_json(spec.model_dump_json()) == spec
