"""#1366 (Argo-readiness epic #1367) — the gang-scheduled distributed-train bridge.

One NEW static bridge class (`CairnTrainFlow`: start → N-rank @parallel →
join → end), selected when the spec's first stage asks for
``resources.workers >= 2``. D1 holds: the class is parameterized
(``num_parallel`` from a runtime Parameter), never generated.

The integration test at the bottom runs a REAL 2-rank gang through the real
bridge locally — proven possible before any of this was written (metaflow
2.19 local @parallel mock; gotcha: it spawns secondary ranks with the bare
command ``python``, hence the PATH handling this suite pins).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cairn.core.dataflow import DataflowSpec, EngineJobHandle

from operator_dataflow_metaflow.engines_metaflow import MetaflowEngine, gang_workers

GANG_SPEC = DataflowSpec.model_validate(
    {"stages": [{"stage": "ingest.normalize", "resources": {"workers": 2, "gpu": 1}}]})
PLAIN_SPEC = DataflowSpec.model_validate({"stages": [{"stage": "ingest.normalize"}]})


def _stub(monkeypatch):
    import cairn.core.dataflow.stage_loader as stages_mod
    monkeypatch.setattr(stages_mod, "load_stages",
                        lambda: {"ingest.normalize": object(), "eval.holdout": object()})
    import operator_dataflow_metaflow.engines_metaflow as em
    monkeypatch.setattr(em, "_metaflow_available", lambda: True)
    monkeypatch.setattr(em, "_lib_available", lambda module: True)


# ── gang size extraction ─────────────────────────────────────────────────────

def test_gang_workers_reads_the_first_stage():
    assert gang_workers(GANG_SPEC) == 2
    assert gang_workers(PLAIN_SPEC) == 1


def test_workers_below_two_is_rejected_by_the_contract():
    """1 is meaningless (that's just 'not distributed') — the schema says so."""
    with pytest.raises(Exception, match="workers"):
        DataflowSpec.model_validate(
            {"stages": [{"stage": "s", "resources": {"workers": 1}}]})


# ── validate: the scope contract ─────────────────────────────────────────────

def test_distributed_flow_must_be_single_stage(monkeypatch):
    """Post-train stages would run N times (duplicated external writes) or
    reopen the #1358 cross-pod working-set problem — pipelines chain legs."""
    _stub(monkeypatch)
    spec = DataflowSpec.model_validate({"stages": [
        {"stage": "ingest.normalize", "resources": {"workers": 2}},
        {"stage": "eval.holdout"},
    ]})
    problems = MetaflowEngine().validate(spec)
    assert any("ONLY the distributed stage" in p for p in problems)
    assert any("pipeline" in p for p in problems)


def test_workers_on_a_later_stage_is_refused_not_ignored(monkeypatch):
    _stub(monkeypatch)
    spec = DataflowSpec.model_validate({"stages": [
        {"stage": "ingest.normalize"},
        {"stage": "eval.holdout", "resources": {"workers": 4}},
    ]})
    assert any("FIRST stage" in p for p in MetaflowEngine().validate(spec))


def test_a_single_distributed_stage_validates_clean(monkeypatch):
    _stub(monkeypatch)
    assert MetaflowEngine().validate(GANG_SPEC) == []


# ── submit routing: which bridge, which env ──────────────────────────────────

class _FakeProc:
    pid = 4242
    returncode = None


@pytest.fixture()
def captured_submit(monkeypatch):
    _stub(monkeypatch)
    captured: dict = {}

    async def _fake_exec(*cmd, **kw):
        captured["cmd"], captured["env"] = list(cmd), kw.get("env") or {}
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return captured


def test_gang_spec_routes_to_the_train_bridge(captured_submit):
    handle = asyncio.run(MetaflowEngine().submit(GANG_SPEC, ctx=None))
    cmd = captured_submit["cmd"]
    assert "operator_dataflow_metaflow.metaflow_train_bridge" in cmd
    assert cmd[cmd.index("--workers") + 1] == "2"
    assert handle.meta["flow_name"] == "CairnTrainFlow"


def test_plain_spec_stays_on_the_one_step_bridge(captured_submit):
    handle = asyncio.run(MetaflowEngine().submit(PLAIN_SPEC, ctx=None))
    cmd = captured_submit["cmd"]
    assert "operator_dataflow_metaflow.metaflow_bridge" in cmd
    assert "--workers" not in cmd
    assert handle.meta["flow_name"] == "CairnFlow"


def test_gang_submit_puts_the_interpreter_bin_on_path(captured_submit):
    """The local @parallel mock spawns ranks with the bare command `python` —
    absent from a uv venv's PATH (found live: FileNotFoundError on rank 1)."""
    asyncio.run(MetaflowEngine().submit(GANG_SPEC, ctx=None))
    first = captured_submit["env"]["PATH"].split(os.pathsep)[0]
    assert first == str(Path(sys.executable).parent)


# ── pathspec: a gang run is CairnTrainFlow/<id> ──────────────────────────────

def test_pathspec_uses_the_handles_flow_name(tmp_path):
    (tmp_path / "run_id.txt").write_text("88", encoding="utf-8")
    eng = MetaflowEngine()
    h = EngineJobHandle(engine="metaflow", job_id="mf-gone",
                        meta={"workdir": str(tmp_path), "flow_name": "CairnTrainFlow"})
    assert eng.pathspec(h) == "CairnTrainFlow/88"


# ── the real thing: a 2-rank gang through the real bridge, locally ───────────

metaflow = pytest.importorskip("metaflow", reason="optional [metaflow] extra")


def test_two_rank_gang_runs_the_real_bridge(tmp_path):
    """REAL metaflow, REAL @parallel, 2 local ranks, the registered noop
    kernel — asserts the join recorded the gang's shape into the stats the
    gates/reports surface reads."""
    workdir = tmp_path / "wd"
    metadata_root = tmp_path / "mfroot"
    metadata_root.mkdir()
    env = dict(os.environ)
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
    env["METAFLOW_USER"] = env["USERNAME"] = "cairn"
    spec = json.dumps({"stages": [{"stage": "noop"}]})
    proc = subprocess.run(
        [sys.executable, "-m", "operator_dataflow_metaflow.metaflow_train_bridge", "run",
         "--spec_json", spec, "--workdir", str(workdir), "--workers", "2"],
        cwd=metadata_root, env=env, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    result = json.loads((workdir / "result.json").read_text(encoding="utf-8"))
    assert result["stats"]["dist.world"] == 2
    assert result["stats"]["dist.ranks_completed"] == 2
    progress = json.loads((workdir / "progress.json").read_text(encoding="utf-8"))
    assert progress["state"] == "succeeded"
