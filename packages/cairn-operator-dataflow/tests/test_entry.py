"""cairn-dataflow-run entrypoint (#1048): spec-from-env, result.json, lineage
stamp, budget/crash exit codes."""
from __future__ import annotations

import json
import os

from cairn.core.dataflow import DataflowSpec, StageRef

from operator_dataflow.engine import entry


def _spec_json(stages=("noop",), **spec_kw) -> str:
    return DataflowSpec(stages=[StageRef(stage=s) for s in stages], **spec_kw).model_dump_json()


def test_env_spec_and_result_written(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CAIRN_DATAFLOW_SPEC", _spec_json(("noop", "noop")))
    monkeypatch.setenv("CAIRN_PARENT_RUN", "run-parent-1")
    assert entry.main([]) == 0
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["stats"]["stages_done"] == 2
    assert result["lineage_parent_run"] == "run-parent-1"
    assert (tmp_path / "metrics.json").exists()


def test_spec_file_arg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CAIRN_DATAFLOW_SPEC", raising=False)
    monkeypatch.delenv("CAIRN_PARENT_RUN", raising=False)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(_spec_json())
    assert entry.main([str(spec_path)]) == 0


def test_unknown_stage_exits_1_with_named_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CAIRN_DATAFLOW_SPEC", _spec_json(("does.not.exist",)))
    assert entry.main([]) == 1
    assert "does.not.exist" in json.loads((tmp_path / "result.json").read_text())["error"]


def test_resume_skips_done_stages(tmp_path, monkeypatch):
    # the .done marker contract: a re-run in the SAME workdir skips completed
    # stages (deterministic outputs make skipping correct — run_fix_pipeline's rule)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CAIRN_DATAFLOW_SPEC", _spec_json(("noop", "noop")))
    assert entry.main([]) == 0
    markers = sorted(p.name for p in tmp_path.glob("*.done"))
    assert len(markers) == 2
    mtimes = {m: os.path.getmtime(tmp_path / m) for m in markers}
    assert entry.main([]) == 0  # second run resumes: markers untouched
    assert {m: os.path.getmtime(tmp_path / m) for m in markers} == mtimes
