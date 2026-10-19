"""#76 Task 0 — a paused HITL gate SURVIVES a process restart (durable spine).

``test_approval_gate_e2e`` proves a gated template pauses + resumes **within one
process** on the default MEMORY checkpointer (state lost on restart). This proves
the durable promise the e2e run depends on: with ``OBS_CHECKPOINTER=sqlite``, a
run paused at the approval ``interrupt()`` is still resumable after the in-process
checkpointer caches are dropped and the template is recompiled from disk against a
fresh runtime — i.e. across a process restart. The only thing shared between the
two "processes" is the sqlite checkpoint file.

Requires the ``runtime-sqlite`` extra (langgraph-checkpoint-sqlite); skipped when
it isn't installed. Restart-survival can't be shown on the memory tier — that
tier loses state by design — so sqlite is mandatory here, not incidental.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

# runtime-sqlite extra — AsyncSqliteSaver is what _run_gated uses under ainvoke.
pytest.importorskip("langgraph.checkpoint.sqlite.aio")

from pydantic import BaseModel

from cairn.core.engine.compiler import compile_template
from cairn.core.operator import (
    Operation,
    OperationContext,
    default_registry,
    register_operator,
)
from cairn.core.pack.manifest import OperatorEntry
from cairn.core.runtime import InProcessRuntime
from cairn.core.runtime.checkpointer import reset_checkpointer


class _GIn(BaseModel):
    pass


class _GOut(BaseModel):
    did_run: bool = True


@register_operator(
    name="test/needs_approval_durable",
    version="0.1.0",
    capabilities=["test.durable"],
)
class _NeedsApprovalDurableOp(Operation):
    input_schema = _GIn
    output_schema = _GOut

    async def run(self, inp, ctx: OperationContext) -> _GOut:
        return _GOut(did_run=True)


def _ensure() -> None:
    name = "test/needs_approval_durable"
    if not default_registry.has(name):
        default_registry.register(_NeedsApprovalDurableOp)
    default_registry.attach_metadata(
        OperatorEntry(
            name=name,
            requires_approval=True,
            provides_capabilities=["test.durable"],
        )
    )


def _gated_template(tmp_path: Path) -> Path:
    """A one-operation template whose single op requires approval, so the
    compiler injects the gate + attaches a checkpointer (ADR-0034)."""
    (tmp_path / "templates").mkdir()
    (tmp_path / "operations").mkdir()
    (tmp_path / "operations" / "gated.yaml").write_text(textwrap.dedent("""\
        operator: test/needs_approval_durable
        input: {}
        output:
          did_run: $state.findings.gated_did_run
    """))
    tmpl = tmp_path / "templates" / "check.md"
    tmpl.write_text(textwrap.dedent("""\
        ---
        name: gated-durable
        version: 0.1.0
        flow:
          type: state_machine
          source: behavior_tree
          root: { type: operation, ref: gated }
          nodes: {}
        tools:
          required: []
        ---
        # gated durable
    """))
    return tmpl


@pytest.mark.asyncio
async def test_paused_gate_survives_restart_on_sqlite(tmp_path, monkeypatch):
    ckpt = tmp_path / "checkpoints.sqlite"
    monkeypatch.setenv("OBS_CHECKPOINTER", "sqlite")
    monkeypatch.setenv("OBS_CHECKPOINTER_PATH", str(ckpt))
    reset_checkpointer()
    _ensure()
    tmpl = _gated_template(tmp_path)

    # ── "process 1": compile + run → pauses at the approval interrupt ──────
    compiled = compile_template(tmpl)
    # gated template → durable checkpointer attached (vs None for ungated).
    assert compiled.runnable._compiled_graph.checkpointer is not None
    rt = InProcessRuntime()
    paused = await rt.invoke(
        compiled.runnable._compiled_graph, {}, run_id="run-durable"
    )
    assert "__interrupt__" in paused
    # The gated operator has NOT executed yet — it's behind the gate.
    assert paused.get("findings", {}).get("gated_did_run") is None
    # The pause was persisted to the sqlite checkpoint file (durable, on disk).
    assert ckpt.exists() and ckpt.stat().st_size > 0

    # ── simulate a RESTART ────────────────────────────────────────────────
    # Drop every in-process checkpointer cache (the per-loop async conns +
    # the sync singleton) AND recompile the template from disk into a brand
    # new graph object. Nothing from "process 1" survives in memory — only
    # the sqlite file on disk does, exactly as after a real server restart.
    reset_checkpointer()
    compiled2 = compile_template(tmpl)
    assert compiled2.runnable._compiled_graph is not compiled.runnable._compiled_graph

    # ── "process 2": resume by thread_id → the gate releases, op runs ──────
    rt2 = InProcessRuntime()
    resumed = await rt2.resume(
        compiled2.runnable._compiled_graph,
        {"approved": True, "approver": "alice"},
        run_id="run-durable",
    )
    # The checkpoint (keyed by thread_id in the shared sqlite file) rehydrated
    # the paused state in the fresh graph, and the approved op finally ran.
    assert resumed.get("findings", {}).get("gated_did_run") is True

    reset_checkpointer()
