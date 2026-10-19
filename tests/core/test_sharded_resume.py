"""Durable shard manifest + resume (#1010, ADR-0078 S4).

run_sharded stages per-shard partials under a deterministic dir with an
atomically-committed manifest: a re-invocation after a crash loads finished
shards and dispatches only the missing ones; a successful merge removes the
stage dir; a changed source file changes the digest so stale partials are
never reused. map_shards_local retries a raising shard and checkpoints per
shard via on_result.

Ops here are plain local Shardables (no registry) — we call the internals
directly with a serial budget so the flow is deterministic and process-free.
"""
from __future__ import annotations

import json
import os

import pytest

from cairn.core.operator import batch, sharded
from cairn.core.operator.sharded import ShardSpec, Shardable


class _SumOp(Shardable):
    """Counts rows in its range; merge sums. Tracks run_shard invocations so
    tests can assert which shards actually (re)ran."""

    name = "test/sum"
    calls: list[int] = []  # class-level — visible across instances in-process

    def shard_total(self, inp) -> int:
        return inp["total"]

    def run_shard(self, inp, spec: ShardSpec):
        _SumOp.calls.append(spec.index)
        return {"n": spec.end - spec.start}

    def merge(self, inp, parts):
        return {"total": sum(p["n"] for p in parts)}


class _Inp:
    """Pydantic-shaped stand-in: model_dump_json + attribute access."""

    def __init__(self, d: dict, path: str | None = None):
        self._d = d
        self.path = path

    def __getitem__(self, k):
        return self._d[k]

    def model_dump_json(self) -> str:
        return json.dumps(self._d, sort_keys=True)


@pytest.fixture(autouse=True)
def _stage_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_STAGE_DIR", str(tmp_path / "stage"))
    monkeypatch.setenv("CAIRN_BATCH_MAX_WORKERS", "1")  # serial — no pool, deterministic
    _SumOp.calls = []
    yield


def _run(op, inp, total, monkeypatch):
    # run_sharded resolves the operator by name in children only when a real
    # pool dispatches; serial budget keeps everything in-process, but the
    # ShardTask path still goes through _child_run_shard → patch resolution.
    monkeypatch.setattr(sharded, "_ensure_operators_loaded", lambda: None)
    monkeypatch.setattr(sharded.default_registry, "get", lambda name: None)

    def _local_child(task):
        return op.run_shard(inp, task.spec)

    monkeypatch.setattr(sharded, "_child_run_shard", _local_child)
    return sharded.run_sharded(op, inp, total)


def test_success_runs_all_shards_and_cleans_stage(tmp_path, monkeypatch):
    op = _SumOp()
    out = _run(op, _Inp({"total": 10}), 10, monkeypatch)
    assert out == {"total": 10}
    stage = tmp_path / "stage"
    assert not any(stage.iterdir())  # stage dir removed after successful merge


def test_resume_skips_committed_shards(tmp_path, monkeypatch):
    op = _SumOp()
    inp = _Inp({"total": 10})

    # First attempt: shard 0 commits, then the run "crashes" inside merge.
    real_merge = op.merge
    monkeypatch.setattr(_SumOp, "merge", lambda self, i, p: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        _run(op, inp, 10, monkeypatch)
    first_calls = list(_SumOp.calls)
    assert first_calls  # something ran and was committed
    stage_dirs = list((tmp_path / "stage").iterdir())
    assert len(stage_dirs) == 1  # kept for the retry (merge failed)
    manifest = json.loads((stage_dirs[0] / "manifest.json").read_text())
    assert len(manifest["shards"]) == len(first_calls)

    # Second attempt: same config → committed shards are LOADED, not re-run.
    monkeypatch.setattr(_SumOp, "merge", real_merge)
    _SumOp.calls = []
    out = _run(op, inp, 10, monkeypatch)
    assert out == {"total": 10}
    assert _SumOp.calls == []  # nothing re-ran — pure resume
    assert not any((tmp_path / "stage").iterdir())  # cleaned after success


def test_changed_source_file_invalidates_stage(tmp_path, monkeypatch):
    src = tmp_path / "data.jsonl"
    src.write_text("v1", encoding="utf-8")
    op = _SumOp()

    monkeypatch.setattr(_SumOp, "merge", lambda self, i, p: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        _run(op, _Inp({"total": 4}, path=str(src)), 4, monkeypatch)
    assert _SumOp.calls  # shards committed under the v1 digest

    # edit the source IN PLACE → digest changes → prior partials must not be reused
    src.write_text("v2 — different content", encoding="utf-8")
    os.utime(src, ns=(1, 1))  # force a distinct mtime even on coarse clocks
    monkeypatch.setattr(_SumOp, "merge", lambda self, i, p: {"total": sum(x["n"] for x in p)})
    _SumOp.calls = []
    out = _run(op, _Inp({"total": 4}, path=str(src)), 4, monkeypatch)
    assert out == {"total": 4}
    assert _SumOp.calls  # everything re-ran — no stale reuse


def test_map_shards_retries_a_raising_shard(monkeypatch):
    monkeypatch.setenv("CAIRN_BATCH_MAX_WORKERS", "1")
    attempts = {"n": 0}

    def flaky(x):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ValueError("transient")
        return x * 2

    seen: list[tuple[int, int]] = []
    out = batch.map_shards_local(flaky, [21], on_result=lambda i, r: seen.append((i, r)))
    assert out == [42] and attempts["n"] == 2
    assert seen == [(0, 42)]  # on_result fired once, after the successful retry


def test_manifest_commit_is_atomic(tmp_path):
    workdir = tmp_path / "w"
    workdir.mkdir()
    sharded._write_manifest(workdir, {"digest": "d", "shards": {}})
    assert json.loads((workdir / "manifest.json").read_text()) == {"digest": "d", "shards": {}}
    assert not [p for p in workdir.iterdir() if ".tmp-" in p.name]


@pytest.mark.parametrize("attempted, loads", [(True, 0), (False, 1)])
def test_child_loads_operators_unless_a_load_was_attempted(monkeypatch, attempted, loads):
    """#2415: a pool child keys its reload on the registry's load-attempted flag
    (inherited under fork, fresh under spawn) — not on a named operator as a probe."""
    from cairn.core.operator import loader
    from cairn.core.operator.registry import OperatorRegistry

    reg = OperatorRegistry()
    if attempted:
        reg.mark_load_attempted()
    calls: list[bool] = []
    monkeypatch.setattr(sharded, "_LOADED", False)
    monkeypatch.setattr(sharded, "default_registry", reg)
    monkeypatch.setattr(loader, "load_all_operators", lambda **k: calls.append(True) or [])
    sharded._ensure_operators_loaded()
    assert len(calls) == loads
