"""#1248 H6a (ADR-0088) — runner checkpoints + resume_from seeding.

Hermetic: a tiny writer stage injected into the registry; every case runs
`run_dataflow` for real over a tmp workdir. The cross-box acceptance (kill
the box, resume elsewhere) is H6d — HERE we prove the runner halves: the
synchronous snapshot after a `checkpoint: true` stage, degrade-on-failure,
and a fresh workdir continuing past a seeded checkpoint via synthetic
markers.
"""
from __future__ import annotations

import json

import pytest

from cairn.core.dataflow import Budget, DataflowSpec, ResumeFrom, StageRef

import cairn.core.dataflow.runner as runner_mod
from cairn.core.dataflow.runner import run_dataflow


CALLS: list[str] = []


def _writer(tag: str):
    def stage(workdir, config, inputs):
        CALLS.append(tag)
        (workdir / "current.jsonl").write_text(
            f'{{"from": "{tag}"}}\n', encoding="utf-8")
        return {"wrote": tag}
    return stage


@pytest.fixture(autouse=True)
def _stages(monkeypatch):
    CALLS.clear()
    monkeypatch.setattr(runner_mod, "load_stages", lambda: {
        "w.one": _writer("one"), "w.two": _writer("two")})


def _spec(**stage_flags) -> DataflowSpec:
    return DataflowSpec(stages=[
        StageRef(stage="w.one", checkpoint=stage_flags.get("one", False)),
        StageRef(stage="w.two", checkpoint=stage_flags.get("two", False)),
    ], budget=Budget())


def test_checkpoint_stage_snapshots_working_set(tmp_path):
    res = run_dataflow(_spec(one=True), tmp_path)
    m = json.loads(
        (tmp_path / "checkpoints" / "stage-00" / "manifest.json").read_text())
    assert m["stage"] == "w.one" and m["stage_index"] == 0
    assert m["files"][0]["name"] == "current.jsonl"
    assert m["files"][0]["content_hash"].startswith("sha256:")
    assert m["stats"] == {"wrote": "one"}                # frozen stats
    snap = tmp_path / "checkpoints" / "stage-00" / "current.jsonl"
    assert snap.read_text() == '{"from": "one"}\n'       # stage-1's state, not 2's
    assert res.stats["checkpoint.last_index"] == 0
    assert (tmp_path / "checkpoints" / "stage-01") .exists() is False  # not declared


def test_failed_snapshot_degrades_never_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "_emit_checkpoint",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    res = run_dataflow(_spec(one=True), tmp_path)
    assert res.stats["stages_done"] == 2                 # the flow COMPLETED
    assert res.stats["checkpoint_failed"].startswith("w.one")


def test_resume_from_runs_only_the_remaining_stages(tmp_path):
    # original run: checkpoint after stage 0
    first = tmp_path / "first"
    run_dataflow(_spec(one=True), first)
    m = json.loads(
        (first / "checkpoints" / "stage-00" / "manifest.json").read_text())
    assert CALLS == ["one", "two"]

    # a FRESH workdir (the other box), seeded from the checkpoint
    CALLS.clear()
    second = tmp_path / "second"
    spec = _spec(one=True)
    spec.resume_from = ResumeFrom(
        spec_key=m["spec_key"], stage_index=0,
        artifacts=[{"uri": f"file://{first}/checkpoints/stage-00/current.jsonl",
                    "content_hash": m["files"][0]["content_hash"],
                    "dest": "current.jsonl"}],
        completed=[{"index": 0, "stage": "w.one", "stats": m["stats"]}],
    )
    res = run_dataflow(spec, second)
    assert CALLS == ["two"]                              # stage one NOT re-run
    assert res.stats["resumed_from_stage"] == 0
    assert res.stats["w.one.wrote"] == "one"             # frozen stats surfaced
    assert res.stats["stages_done"] == 2


def test_changed_spec_invalidates_the_resume(tmp_path):
    spec = _spec()
    spec.resume_from = ResumeFrom(spec_key="stale00", stage_index=0,
                                  completed=[{"index": 0, "stage": "w.one",
                                              "stats": {}}])
    res = run_dataflow(spec, tmp_path)
    assert CALLS == ["one", "two"]                       # full restart
    assert res.stats["resume_invalid_spec"] is True


def test_resume_key_is_stable_across_resubmission(tmp_path):
    """The spec_key must EXCLUDE resume_from, or the resumed submission would
    hash differently from the original and self-invalidate every checkpoint."""
    first = tmp_path / "a"
    run_dataflow(_spec(one=True), first)
    m = json.loads(
        (first / "checkpoints" / "stage-00" / "manifest.json").read_text())
    spec = _spec(one=True)
    spec.resume_from = ResumeFrom(spec_key=m["spec_key"], stage_index=0,
                                  completed=[{"index": 0, "stage": "w.one",
                                              "stats": {}}])
    res = run_dataflow(spec, tmp_path / "b")
    assert "resume_invalid_spec" not in res.stats
