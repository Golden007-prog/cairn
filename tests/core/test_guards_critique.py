"""Tests for the opt-in self-critique / reflection loop.

The loop's contract: accept good output without a revise, revise low-confidence
output up to a bound, and never exceed ``budget.max_iterations``. The critique
LLM call is monkeypatched so these run offline and deterministically.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("OPENAI_API_KEY", "sk-validate-offline")

import pytest

from cairn.core.guards import critique as critique_mod
from cairn.core.guards.critique import CritiqueVerdict, run_critique_loop
from cairn.core.engine.schema import CritiqueConfig


class _FakeCompiled:
    """Duck-typed CompiledTemplate: counts re-invocations, returns versioned output."""

    def __init__(self, critique_config):
        self.spec = SimpleNamespace(critique=critique_config, budget=None)
        self.runs = 0

    async def run(self, input_data):
        self.runs += 1
        return {"output": f"revision-{self.runs}", "type": "agent_loop"}


def _patch_verdict(monkeypatch, *verdicts):
    """Make _critique_once yield the given verdicts in order (last repeats)."""
    seq = list(verdicts)

    async def fake(output_text, config):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(critique_mod, "_critique_once", fake)


_INPUT = {"symptom": "x", "user_message": "investigate"}


@pytest.mark.asyncio
async def test_disabled_is_noop(monkeypatch):
    compiled = _FakeCompiled(CritiqueConfig(enabled=False))
    out = await run_critique_loop(compiled, {"output": "v0"}, _INPUT)
    assert out == {"output": "v0"}
    assert "critique_trail" not in out
    assert compiled.runs == 0


@pytest.mark.asyncio
async def test_no_config_is_noop(monkeypatch):
    compiled = _FakeCompiled(None)
    out = await run_critique_loop(compiled, {"output": "v0"}, _INPUT)
    assert out == {"output": "v0"}


@pytest.mark.asyncio
async def test_acceptable_first_pass_no_revise(monkeypatch):
    _patch_verdict(monkeypatch, CritiqueVerdict(acceptable=True, confidence=0.95))
    compiled = _FakeCompiled(CritiqueConfig(max_passes=3))
    out = await run_critique_loop(compiled, {"output": "v0"}, _INPUT)
    assert compiled.runs == 0  # never re-invoked
    assert out["critique_passes"] == 1
    assert out["critique_accepted"] is True


@pytest.mark.asyncio
async def test_confidence_threshold_accepts(monkeypatch):
    # not "acceptable" but confidence above the 0.7 bar → accept, no revise
    _patch_verdict(monkeypatch, CritiqueVerdict(acceptable=False, confidence=0.8))
    compiled = _FakeCompiled(CritiqueConfig(max_passes=3, confidence_threshold=0.7))
    out = await run_critique_loop(compiled, {"output": "v0"}, _INPUT)
    assert compiled.runs == 0
    assert out["critique_accepted"] is True


@pytest.mark.asyncio
async def test_low_confidence_revises_up_to_max_passes(monkeypatch):
    _patch_verdict(monkeypatch, CritiqueVerdict(acceptable=False, confidence=0.1, issues=["weak"]))
    compiled = _FakeCompiled(CritiqueConfig(max_passes=2))
    out = await run_critique_loop(compiled, {"output": "v0"}, _INPUT)
    assert compiled.runs == 2          # revised twice
    assert out["critique_passes"] == 2
    assert out["critique_accepted"] is False
    assert out["output"] == "revision-2"  # output replaced by last revise


@pytest.mark.asyncio
async def test_budget_max_iterations_caps_passes(monkeypatch):
    _patch_verdict(monkeypatch, CritiqueVerdict(acceptable=False, confidence=0.0))
    compiled = _FakeCompiled(CritiqueConfig(max_passes=5))
    out = await run_critique_loop(compiled, {"output": "v0"}, _INPUT, max_iterations_cap=1)
    assert compiled.runs == 1          # capped at 1 despite max_passes=5
    assert out["critique_passes"] == 1


@pytest.mark.asyncio
async def test_feedback_injected_into_revise_input(monkeypatch):
    _patch_verdict(
        monkeypatch,
        CritiqueVerdict(acceptable=False, confidence=0.0, issues=["missing traces"],
                        suggested_revision="check traces"),
        CritiqueVerdict(acceptable=True, confidence=0.9),
    )
    seen_inputs = []

    class _Recorder(_FakeCompiled):
        async def run(self, input_data):
            seen_inputs.append(input_data["user_message"])
            return await super().run(input_data)

    compiled = _Recorder(CritiqueConfig(max_passes=3))
    await run_critique_loop(compiled, {"output": "v0"}, _INPUT)
    assert compiled.runs == 1
    assert "missing traces" in seen_inputs[0]
    assert "check traces" in seen_inputs[0]
