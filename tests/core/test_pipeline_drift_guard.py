"""ADR-0087 P3/D6 (#1255) — run-by-ref drift guard + the SDK run surface.

The local class is a pointer with a checksum: ``run()`` submits the installed
record pinned to the build's hash, and the server refuses on mismatch — CI and
the console can never silently run different pipelines.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.dispatch.pipeline_launch import PipelineDriftError, launch_pipeline
from cairn.core.engine.pipeline import load_pipeline_def
from cairn_sdk import compile_pipeline, inp, pipeline, step
from cairn_sdk.client import PipelineRun, run_pipeline
from cairn_sdk.client import PipelineDriftError as SdkDriftError


@pipeline(series="drift")
class Tiny:
    """One-leg pipeline for hash agreement tests."""

    class Inputs:
        dataset: str = "d.jsonl"

    check = step("check", raw=inp.dataset)


@pytest.fixture(autouse=True)
def _refs_resolve(monkeypatch):
    """#1538 — assert_runnable now resolves every leg at launch. Tiny's leg is
    a pack-local `flow: tiny/check` ref that exists nowhere (the pack lives in
    tmp, uninstalled). This file's subject is the HASH pin, not resolution —
    which has its own suite — so stub the flow lookup to isolate the guard."""
    import cairn.core.engine.pipeline_resolve as R
    monkeypatch.setattr(R, "_flow_def_exists", lambda ref: True)


def _install(tmp_path):
    compiled = compile_pipeline(Tiny)
    pack = tmp_path / "drift-pack"
    compiled.write(pack)
    return compiled, load_pipeline_def(pack / "pipelines" / "tiny.yaml")


# ── hash agreement: the SDK's pin equals the loader's source identity ───────

def test_compiled_hash_equals_loader_source_hash(tmp_path):
    """The whole D6 contract in one assertion: the hash the SDK computes over
    its build artifact IS the hash the loader stamps on the installed file —
    with neither side importing the other."""
    compiled, d = _install(tmp_path)
    assert compiled.content_hash == d.source_hash
    assert d.content_hash and d.content_hash != d.source_hash  # two identities, both real


def test_source_hash_excluded_from_content_hash(tmp_path):
    """Adding source_hash must not have changed any existing content_hash —
    it is excluded from the canonical dump exactly like content_hash itself.

    This used to restate the hash formula inline, and so stopped describing the
    implementation the moment the canonical one grew the ADR-0107 display-field
    exclusions (#2145): the test failed while both the hash and the exclusion
    were correct. It now reads the engine's own :func:`identity_dump`, and keeps
    its teeth through the two assertions below — neither of which a formula copy
    was making.
    """
    import hashlib
    import json

    from cairn.core.engine.pipeline import identity_dump

    _, d = _install(tmp_path)
    expect = "sha256:" + hashlib.sha256(
        json.dumps(identity_dump(d), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    assert d.content_hash == expect
    # the actual claim: neither identity is part of the other
    assert "source_hash" not in identity_dump(d)
    assert "content_hash" not in identity_dump(d)


# ── the launch-door guard ───────────────────────────────────────────────────

class _Bridge:
    async def start_pipeline(self, plan, **kw):
        return type("Rec", (), {"pipeline_id": "pl_x", "status": "running"})()


async def _launch(monkeypatch, d, expected_hash):
    monkeypatch.setattr(
        "cairn.core.dispatch.pipeline_launch.resolve_or_raise", lambda ref: d)
    monkeypatch.setattr(
        "cairn.core.policy.pipeline_gates.inject_policy_gates",
        lambda plan, environment=None, engine=None: [])
    return await launch_pipeline(
        "drift-pack/tiny", inputs={}, bridge=_Bridge(), expected_hash=expected_hash)


async def test_matching_pin_launches(monkeypatch, tmp_path):
    compiled, d = _install(tmp_path)
    rec = await _launch(monkeypatch, d, compiled.content_hash)
    assert rec.status == "running"


async def test_content_hash_pin_also_accepted(monkeypatch, tmp_path):
    _, d = _install(tmp_path)
    rec = await _launch(monkeypatch, d, d.content_hash)
    assert rec.status == "running"


async def test_drifted_pin_is_refused_before_any_record(monkeypatch, tmp_path):
    _, d = _install(tmp_path)
    with pytest.raises(PipelineDriftError, match="rebuild and reinstall"):
        await _launch(monkeypatch, d, "sha256:deadbeefdeadbeef")


async def test_no_pin_keeps_todays_behavior(monkeypatch, tmp_path):
    _, d = _install(tmp_path)
    rec = await _launch(monkeypatch, d, None)
    assert rec.status == "running"


# ── the SDK run surface (stubbed client — no HTTP) ──────────────────────────

class _StubClient:
    def __init__(self, states):
        self._states = list(states)
        self.launches: list[dict] = []
        self.approvals: list[tuple[str, str]] = []

    def launch_pipeline(self, ref, inputs, *, environment=None, expected_hash=None):
        self.launches.append({"ref": ref, "expected_hash": expected_hash})
        return {"pipeline_id": "pl_1", "status": "running"}

    def pipeline(self, pipeline_id):
        return self._states[0] if len(self._states) == 1 else self._states.pop(0)

    def approve_step(self, pipeline_id, step_id, *, approver=None):
        self.approvals.append((step_id, approver))
        return {"status": "running"}

    def cancel_pipeline(self, pipeline_id):
        return {"status": "cancelled"}


def test_run_pins_the_compiled_hash(tmp_path):
    compiled, _ = _install(tmp_path)
    client = _StubClient([{"status": "running", "step_state": {}}])
    run_pipeline(client, "drift-pack/tiny", compiled=compiled)
    assert client.launches[0]["expected_hash"] == compiled.content_hash


def test_wait_surfaces_gates_then_returns_terminal():
    client = _StubClient([
        {"status": "paused",
         "step_state": {"approve": {"status": "awaiting_approval"}},
         "step_order": ["approve"]},
        {"status": "done", "step_state": {"approve": {"status": "done"}}},
    ])
    run = PipelineRun(client, "pl_1")
    fired: list[str] = []

    def _on_gate(g):
        fired.append(g.step_id)
        g.approve(approver="ci")

    out = run.wait(timeout_s=5, poll_s=0.01, on_gate=_on_gate)
    assert out["status"] == "done"
    assert fired == ["approve"]
    assert client.approvals == [("approve", "ci")]
