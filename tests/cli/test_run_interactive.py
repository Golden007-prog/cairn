"""#56 — `cairn run --interactive`: in-process HITL resume for a bare local run.

A gated template run via direct-template mode pauses at the gate; with
--interactive the CLI prompts and resumes IN-PROCESS (no server). Mocks the
prompt; pins approve→completes, reject→terminates, non-interactive→reports paused.
"""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest
from pydantic import BaseModel

from cairn.cli.commands import run as run_cmd
from cairn.core.operator import (
    Operation, OperationContext, default_registry, register_operator,
)
from cairn.core.pack.manifest import OperatorEntry


class _In(BaseModel):
    pass


class _Out(BaseModel):
    did_run: bool = True


@register_operator(name="test/run_gate_op", version="0.1.0", capabilities=["test.rg"])
class _GateOp(Operation):
    input_schema = _In
    output_schema = _Out

    async def run(self, inp, ctx: OperationContext) -> _Out:
        return _Out(did_run=True)


def _pack(tmp_path: Path) -> Path:
    (tmp_path / "templates").mkdir()
    (tmp_path / "operations").mkdir()
    (tmp_path / "operations" / "gate.yaml").write_text(
        "operator: test/run_gate_op\ninput: {}\noutput:\n  did_run: $state.ran\n"
    )
    (tmp_path / "templates" / "gated.md").write_text(
        "---\nname: gated\nversion: 0.1.0\n"
        "flow:\n  type: state_machine\n  source: behavior_tree\n"
        "  root: { type: operation, ref: gate }\n  nodes: {}\n"
        "tools:\n  required: []\n---\n# gated\n"
    )
    return tmp_path / "templates" / "gated.md"


@pytest.fixture
def wired(monkeypatch, tmp_path):
    # _run_template_async clears the registry + reloads official operators;
    # no-op the clear so our gated test operator survives.
    monkeypatch.setattr(default_registry, "clear", lambda: None)
    if not default_registry.has("test/run_gate_op"):
        default_registry.register(_GateOp)
    default_registry.attach_metadata(
        OperatorEntry(name="test/run_gate_op", requires_approval=True,
                      provides_capabilities=["test.rg"])
    )
    tmpl = _pack(tmp_path)

    import cairn.core.engine.skill_engine as se
    orig = se.resolve_template_path
    monkeypatch.setattr(
        se, "resolve_template_path",
        lambda name, *, from_pack=None: tmpl if name in ("gated", "test/gated")
        else orig(name, from_pack=from_pack),
    )
    inp = tmp_path / "in.json"
    inp.write_text("{}")
    return inp


async def test_interactive_approve_resumes_to_completion(wired, monkeypatch):
    monkeypatch.setattr(run_cmd.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(run_cmd, "_prompt_hitl",
                        lambda output: {"approved": True, "approver": "t"})
    _, rc = await run_cmd._run_template_async("gated", wired, environment="production", interactive=True)
    assert rc == 0  # approved → the gated op ran → terminal


async def test_interactive_reject_still_terminates(wired, monkeypatch):
    monkeypatch.setattr(run_cmd.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(run_cmd, "_prompt_hitl",
                        lambda output: {"approved": False, "approver": "t"})
    _, rc = await run_cmd._run_template_async("gated", wired, environment="production", interactive=True)
    assert rc == 0  # rejected → op skipped, but the run reaches a terminal state


async def test_non_interactive_gated_run_reports_paused(wired):
    _, rc = await run_cmd._run_template_async("gated", wired, environment="production", interactive=False)
    assert rc == 1  # paused at the gate, never resumed → not "done"
