"""#1358 (ADR-0089) — a REMOTE task's result must cross the machine boundary.

The bridge writes `result.json` into `--workdir` and the engine reads it back
from that same local path. For a local subprocess that is one filesystem. For a
`@kubernetes`/`@batch` task it is two: the pod writes into its own container and
the control plane sees nothing, so a run whose kernels all SUCCEEDED came back
as FileNotFoundError. Found on a live k3s cluster, not by a test.

Metaflow's datastore is the channel that does cross (and #1349 already forces
s3 for remote), so the engine falls back to the `result_payload` ARTIFACT the
bridge publishes. Local runs must keep reading the file — that path is not
allowed to acquire a datastore round-trip.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cairn.core.dataflow import EngineError, EngineJobHandle

from operator_dataflow_metaflow.engines_metaflow import MetaflowEngine

PAYLOAD = {"stats": {"ingest.normalize.rows": 12}, "output_artifacts": [],
           "warnings": [], "summary": "ok"}


def _handle(workdir: Path) -> EngineJobHandle:
    return EngineJobHandle(engine="metaflow", job_id="mf-1",
                           meta={"workdir": str(workdir)})


def _local_result_file(tmp_path: Path, payload: dict) -> Path:
    (tmp_path / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


# ── local: unchanged, and no datastore call ──────────────────────────────────

def test_local_reads_the_file_and_never_touches_the_datastore(tmp_path, monkeypatch):
    eng = MetaflowEngine()                      # compute=local → not strict
    called = []
    monkeypatch.setattr(eng, "_result_from_datastore",
                        lambda h: called.append(1) or None)
    _local_result_file(tmp_path, PAYLOAD)
    res = asyncio.run(eng.result(_handle(tmp_path)))
    assert res.stats["ingest.normalize.rows"] == 12
    assert called == [], "local must not pay a datastore round-trip"


def test_a_local_run_with_no_file_still_fails_loudly(tmp_path):
    with pytest.raises(EngineError, match="result.json missing"):
        asyncio.run(MetaflowEngine().result(_handle(tmp_path)))


# ── remote: the fallback that #1358 is about ─────────────────────────────────

def _remote(monkeypatch, payload):
    eng = MetaflowEngine(compute={"kind": "kubernetes", "image": "i:1"},
                         datastore="s3", datastore_root="s3://b/c")
    monkeypatch.setattr(eng, "_result_from_datastore", lambda h: payload)
    return eng


def test_remote_falls_back_to_the_datastore_artifact(tmp_path, monkeypatch):
    """The pod's result.json is unreachable — the artifact carries it."""
    eng = _remote(monkeypatch, PAYLOAD)
    res = asyncio.run(eng.result(_handle(tmp_path)))   # tmp_path has NO file
    assert res.stats["ingest.normalize.rows"] == 12


def test_remote_prefers_a_local_file_when_one_exists(tmp_path, monkeypatch):
    """Belt and braces: a shared-filesystem remote setup keeps the cheap path."""
    eng = _remote(monkeypatch, {"stats": {"from": "datastore"}, "output_artifacts": []})
    _local_result_file(tmp_path, PAYLOAD)
    res = asyncio.run(eng.result(_handle(tmp_path)))
    assert res.stats["ingest.normalize.rows"] == 12


def test_an_unreadable_datastore_names_the_run_not_a_missing_file(tmp_path, monkeypatch):
    """The old message ('result.json missing') sent you looking at the wrong
    machine entirely."""
    eng = _remote(monkeypatch, {"error": "could not read result from the metaflow "
                                         "datastore (CairnFlow/123): S3Error: denied"})
    with pytest.raises(EngineError, match="metaflow datastore"):
        asyncio.run(eng.result(_handle(tmp_path)))


def test_remote_with_nothing_anywhere_still_fails(tmp_path, monkeypatch):
    eng = _remote(monkeypatch, None)
    with pytest.raises(EngineError):
        asyncio.run(eng.result(_handle(tmp_path)))


# ── the bridge's write must survive a fresh pod filesystem ───────────────────

def test_bridge_write_creates_a_missing_parent(tmp_path):
    """`end` runs in a DIFFERENT pod from `start`, so the workdir it was told
    to use does not exist there. Before #1358 that killed the flow after every
    kernel had already succeeded."""
    pytest.importorskip("metaflow")  # metaflow_bridge is a FlowSpec module; [metaflow] extra, as the sibling tests
    from operator_dataflow_metaflow.metaflow_bridge import _write

    target = tmp_path / "never-created" / "result.json"
    _write(target, {"ok": True})
    assert json.loads(target.read_text())["ok"] is True
