"""#1442 / #1432 — the runner emits REAL descriptors: a stage that reports its
product (`output` path/URI + `content_hash`) gets that as its descriptor; each
stage's parent is the previous stage's descriptor; rows/bytes carried; stages
that only rewrite the working file keep the idempotent placeholder form."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairn.core.dataflow import DataflowSpec, StageRef

import cairn.core.dataflow.runner as runner_mod
from cairn.core.dataflow.runner import run_dataflow


@pytest.fixture(autouse=True)
def _stages(monkeypatch):
    def write(workdir: Path, config: dict, inputs: list[str]) -> dict:
        (workdir / "current.jsonl").write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
        return {"rows": 2}

    def rewrite(workdir: Path, config: dict, inputs: list[str]) -> dict:
        return {"rows": 2, "dropped": 0}

    def export(workdir: Path, config: dict, inputs: list[str]) -> dict:
        return {"output": config.get("uri", "s3://bkt/pfx/dataflow/x/out.jsonl"),
                "content_hash": "c" * 64, "bytes": 14, "rows": 2, "format": "jsonl"}
    monkeypatch.setattr(runner_mod, "load_stages", lambda: {"t.write": write, "t.rewrite": rewrite, "t.export": export})


def test_descriptor_chain_uri_hash_parents(tmp_path: Path):
    spec = DataflowSpec(
        stages=[StageRef(stage="t.write"), StageRef(stage="t.rewrite"), StageRef(stage="t.export")],
        input_artifacts=["s3://bkt/in/raw.jsonl"],
    )
    res = run_dataflow(spec, tmp_path)
    d = res.output_artifacts
    assert len(d) == 3
    # stage 0: placeholder uri, parent = the flow's declared input
    assert d[0].uri.startswith("file://") and d[0].uri.endswith("stage-00")
    assert d[0].parents == ["s3://bkt/in/raw.jsonl"] and d[0].rows == 2
    # stage 1: placeholder, parent = stage 0's registry ID (content hash)
    assert d[1].parents == [d[0].content_hash]
    # stage 2 (export): REAL uri + content hash + bytes, parent = stage 1
    assert d[2].uri == "s3://bkt/pfx/dataflow/x/out.jsonl"
    assert d[2].content_hash == "c" * 64 and d[2].size_bytes == 14 and d[2].rows == 2
    assert d[2].parents == [d[1].content_hash]
    # placeholder hashes stay content-addressed off (spec, index) → idempotent re-runs
    assert d[0].content_hash.endswith("-00") and d[1].content_hash.endswith("-01")


def test_local_export_path_becomes_file_uri(tmp_path: Path):
    spec = DataflowSpec(stages=[StageRef(stage="t.write"),
                                StageRef(stage="t.export", config={"uri": str(tmp_path / "out.jsonl")})])
    res = run_dataflow(spec, tmp_path)
    assert res.output_artifacts[-1].uri == f"file://{tmp_path / 'out.jsonl'}"


def test_stage0_parent_is_the_ingest_source_uri(tmp_path: Path, monkeypatch):
    """#1432 cross-leg lineage: an ingest stage that reports what it read
    (`source`, e.g. the upstream leg's S3 export) makes that URI stage 0's
    parent; the control plane resolves it to the registry id."""
    def ingest(workdir: Path, config: dict, inputs: list[str]) -> dict:
        (workdir / "current.jsonl").write_text('{"a":1}\n', encoding="utf-8")
        return {"rows": 1, "source": "s3://bkt/campaign/dataflow/prev/train-ready.jsonl"}
    monkeypatch.setattr(runner_mod, "load_stages", lambda: {"t.ingest": ingest})
    res = run_dataflow(DataflowSpec(stages=[StageRef(stage="t.ingest")]), tmp_path)
    assert res.output_artifacts[0].parents == ["s3://bkt/campaign/dataflow/prev/train-ready.jsonl"]


def test_stage0_parent_prefers_the_source_content_hash(tmp_path: Path, monkeypatch):
    """The registry is content-addressed: the upstream export's id IS its
    sha256, so an ingest that reports `source_hash` gives stage 0 a parent the
    lineage DAG can walk with no URI lookup (a re-exported identical file keeps
    the first record's URI, which made URI resolution miss — found live)."""
    def ingest(workdir: Path, config: dict, inputs: list[str]) -> dict:
        (workdir / "current.jsonl").write_text('{"a":1}\n', encoding="utf-8")
        return {"rows": 1, "source": "s3://bkt/x.jsonl", "source_hash": "d" * 64, "source_bytes": 8}
    monkeypatch.setattr(runner_mod, "load_stages", lambda: {"t.ingest": ingest})
    res = run_dataflow(DataflowSpec(stages=[StageRef(stage="t.ingest")]), tmp_path)
    assert res.output_artifacts[0].parents == ["d" * 64]


def test_a_stage_that_declares_kind_model_gets_a_model_descriptor(tmp_path: Path, monkeypatch):
    """#2189 (T04): `package.adapter` says what its product is; the runner carries it."""
    def package(workdir: Path, config: dict, inputs: list[str]) -> dict:
        return {"output": "hf://org/adapter", "content_hash": "d" * 64, "bytes": 42, "kind": "model"}
    def bogus(workdir: Path, config: dict, inputs: list[str]) -> dict:
        return {"output": "/tmp/x", "content_hash": "e" * 64, "kind": "sculpture"}
    monkeypatch.setattr(runner_mod, "load_stages", lambda: {"t.package": package, "t.bogus": bogus})
    res = run_dataflow(DataflowSpec(stages=[StageRef(stage="t.package"), StageRef(stage="t.bogus")]), tmp_path)
    assert res.output_artifacts[0].kind == "model" and res.output_artifacts[0].uri == "hf://org/adapter"
    assert res.output_artifacts[1].kind == "dataset"     # an unknown kind falls back, never raises
