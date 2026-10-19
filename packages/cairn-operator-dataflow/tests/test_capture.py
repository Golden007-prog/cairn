"""Uniform run capture (#1465) — hermetic tier: uploads stubbed, no boto3."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairn.core.dataflow import capture


@pytest.fixture()
def bucket_env(monkeypatch):
    monkeypatch.setenv("OBS_ARTIFACT_S3_BUCKET", "test-bucket")
    monkeypatch.setenv("OBS_ARTIFACT_S3_PREFIX", "pfx")
    monkeypatch.delenv("CAIRN_CAPTURE", raising=False)


@pytest.fixture()
def uploads(monkeypatch):
    calls: list[tuple[str, str]] = []
    indexes: dict[str, bytes] = {}
    monkeypatch.setattr(capture, "_upload", lambda src, uri: calls.append((str(src), uri)))
    monkeypatch.setattr(capture, "_upload_bytes", lambda data, uri: indexes.__setitem__(uri, data))
    calls_and_indexes = (calls, indexes)
    return calls_and_indexes


def test_disabled_without_bucket(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OBS_ARTIFACT_S3_BUCKET", raising=False)
    assert capture.enabled() is False
    assert capture.snapshot_workdir(tmp_path) == {}
    assert capture.capture_stage(tmp_path, {}, 0, "x") is None


def test_opt_out_flag(tmp_path: Path, bucket_env, monkeypatch):
    monkeypatch.setenv("CAIRN_CAPTURE", "off")
    assert capture.enabled() is False


def test_new_and_changed_files_captured(tmp_path: Path, bucket_env, uploads):
    (tmp_path / "current.jsonl").write_text("old\n")
    before = capture.snapshot_workdir(tmp_path)
    # the stage rewrites the working file and creates a product
    (tmp_path / "current.jsonl").write_text("new rows\n")
    (tmp_path / "baseline_answers.jsonl").write_text('{"q": 1}\n')
    out = capture.capture_stage(tmp_path, before, 2, "eval.closedbook")
    calls, indexes = uploads
    assert out["artifacts_count"] == 2
    root = "s3://test-bucket/pfx/dataflow/%s/capture/02-eval.closedbook" % tmp_path.name
    assert out["artifacts_index"] == root + "/captured.json"
    idx = json.loads(indexes[out["artifacts_index"]])
    names = {a["name"] for a in idx["artifacts"]}
    assert names == {"current.jsonl", "baseline_answers.jsonl"}
    a = next(x for x in idx["artifacts"] if x["name"] == "baseline_answers.jsonl")
    assert a["uri"] == root + "/baseline_answers.jsonl"
    assert a["bytes"] == len('{"q": 1}\n') and len(a["content_hash"]) == 64
    assert {u for _, u in calls} == {x["uri"] for x in idx["artifacts"]}


def test_untouched_and_internal_files_skipped(tmp_path: Path, bucket_env, uploads):
    (tmp_path / "untouched.jsonl").write_text("same\n")
    before = capture.snapshot_workdir(tmp_path)
    (tmp_path / "metrics.json").write_text("{}")            # runner bookkeeping
    (tmp_path / "result.json").write_text("{}")
    (tmp_path / "stage-00-ingest.done").write_text("{}")
    (tmp_path / ".hidden").write_text("x")
    assert capture.capture_stage(tmp_path, before, 0, "ingest.normalize") is None
    assert uploads[0] == []


def test_size_cap_counts_skips(tmp_path: Path, bucket_env, uploads, monkeypatch):
    monkeypatch.setenv("CAIRN_CAPTURE_MAX_BYTES", "10")
    before = capture.snapshot_workdir(tmp_path)
    (tmp_path / "big.bin").write_bytes(b"x" * 64)
    (tmp_path / "small.txt").write_text("ok")
    out = capture.capture_stage(tmp_path, before, 1, "prep")
    assert out["artifacts_count"] == 1 and out["artifacts_skipped"] == 1
    idx = json.loads(uploads[1][out["artifacts_index"]])
    assert [a["name"] for a in idx["artifacts"]] == ["small.txt"]


def test_upload_failure_never_raises(tmp_path: Path, bucket_env, monkeypatch):
    def boom(src, uri):
        raise RuntimeError("s3 down")
    monkeypatch.setattr(capture, "_upload", boom)
    before = capture.snapshot_workdir(tmp_path)
    (tmp_path / "product.jsonl").write_text("row\n")
    assert capture.capture_stage(tmp_path, before, 0, "x") is None  # logged, not raised


def test_runner_merges_descriptors_into_stats(tmp_path: Path, bucket_env, uploads, monkeypatch):
    """End-to-end through run_dataflow: a stage's created file lands in its
    stats (flattened as <stage>.artifacts) and in the .done marker."""
    from cairn.core.dataflow import DataflowSpec
    from cairn.core.dataflow import runner

    def fake_stage(workdir: Path, config: dict, inputs: list[str]) -> dict:
        (workdir / "answers.jsonl").write_text('{"a": 1}\n')
        return {"rows": 1}

    monkeypatch.setattr(runner, "load_stages", lambda: {"fake.stage": fake_stage})
    spec = DataflowSpec.model_validate({"stages": [{"stage": "fake.stage", "config": {}}]})
    result = runner.run_dataflow(spec, tmp_path / "wd")
    assert result.stats["fake.stage.artifacts_count"] == 1
    index_uri = result.stats["fake.stage.artifacts_index"]
    idx = json.loads(uploads[1][index_uri])
    assert [a["name"] for a in idx["artifacts"]] == ["answers.jsonl"]
    marker = json.loads((tmp_path / "wd" / "stage-00-fake.stage.done").read_text())
    assert marker["artifacts_count"] == 1 and marker["rows"] == 1
