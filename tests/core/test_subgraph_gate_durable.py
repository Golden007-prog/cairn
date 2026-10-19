"""#532 Problem 2b — a composed-subgraph HITL gate must resume across a FRESH
graph instance via a DURABLE checkpointer (the server↔worker / restart scenario).

The in-process memory case is covered by test_subgraph_gate.py. That test shares
one graph object + the memory singleton, so it can't catch the box failure:
there the interrupt was written by the worker and the resume issued against a
DIFFERENT process. This test reproduces that shape with the sqlite checkpointer —
pause on one compiled graph, resume on a freshly-compiled one that only shares
the backing store — proving durable cross-instance resume works and that #532's
symptom was the memory-checkpointer fallback (Problem 2a), not the gate wiring.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest
from pydantic import BaseModel

pytest.importorskip("langgraph.checkpoint.sqlite.aio")  # runtime-sqlite extra

from cairn.core.engine.compiler import compile_template
from cairn.core.operator import Operation, OperationContext, default_registry, register_operator
from cairn.core.pack.manifest import OperatorEntry
from cairn.core.runtime import InProcessRuntime
from cairn.core.runtime import checkpointer as ckpt


class _In(BaseModel):
    pass


class _Out(BaseModel):
    did_run: bool = True


@register_operator(name="test/durable_gate_op", version="0.1.0", capabilities=["test.dg"])
class _GateOp(Operation):
    input_schema = _In
    output_schema = _Out

    async def run(self, inp, ctx: OperationContext) -> _Out:
        return _Out(did_run=True)


def _ensure_op():
    if not default_registry.has("test/durable_gate_op"):
        default_registry.register(_GateOp)
    default_registry.attach_metadata(OperatorEntry(
        name="test/durable_gate_op", requires_approval=True,
        provides_capabilities=["test.dg"]))


def _build_pack(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "operations").mkdir()
    (tmp_path / "operations" / "gate-op.yaml").write_text(
        "operator: test/durable_gate_op\ninput: {}\noutput:\n  did_run: $state.ran\n")
    (tmp_path / "templates" / "childdur.md").write_text(
        "---\nname: child-dur\nversion: 0.1.0\nflow:\n  type: state_machine\n"
        "  source: behavior_tree\n  root: { type: operation, ref: gate-op }\n"
        "  nodes: {}\ntools:\n  required: []\n---\n# child\n")
    (tmp_path / "templates" / "parentdur.md").write_text(
        "---\nname: parent-dur\nversion: 0.1.0\nflow:\n  type: state_machine\n"
        "  source: behavior_tree\n  root:\n    type: sequence\n    children:\n"
        "      - type: subgraph_node\n        template: childdur\n        inputs: {}\n"
        "        outputs:\n          ran: parent_ran\n  nodes: {}\ntools:\n  required: []\n---\n# parent\n")
    return tmp_path / "templates" / "parentdur.md"


@pytest.fixture
def durable_env(monkeypatch, tmp_path):
    """sqlite checkpointer at a tmp file + resolver for `template: <name>`."""
    monkeypatch.setenv("OBS_CHECKPOINTER", "sqlite")
    monkeypatch.setenv("OBS_CHECKPOINTER_PATH", str(tmp_path / "ckpt.db"))
    # drop any singletons a prior test warmed, so we read THIS file.
    ckpt._DURABLE_SINGLETON = None
    ckpt._SQLITE_SAVER_BY_LOOP.clear()
    ckpt._SQLITE_CM_BY_LOOP.clear()

    from cairn.core.engine import skill_engine
    orig = skill_engine.resolve_template_path

    def _resolve(name, *, from_pack=None):
        m = tmp_path / "templates" / f"{name}.md"
        return m if m.exists() else orig(name, from_pack=from_pack)

    monkeypatch.setattr(skill_engine, "resolve_template_path", _resolve)
    yield tmp_path
    ckpt._DURABLE_SINGLETON = None
    ckpt._SQLITE_SAVER_BY_LOOP.clear()
    ckpt._SQLITE_CM_BY_LOOP.clear()


async def test_durable_subgraph_gate_resumes_on_a_fresh_instance(durable_env, tmp_path):
    _ensure_op()
    parent_path = _build_pack(tmp_path)

    # Instance A compiles + runs → pauses at the child gate (checkpoint → sqlite).
    parent_a = compile_template(parent_path)
    assert parent_a.runnable._compiled_graph.checkpointer is not None
    paused = await parent_a.run({"run_id": "dur-1"})
    assert "__interrupt__" in paused
    assert paused.get("parent_ran") is None

    # Instance B is a SEPARATE compile (no shared graph object / memory saver) —
    # it shares only the sqlite backing store. This is the server↔worker shape.
    parent_b = compile_template(parent_path)
    rt = InProcessRuntime()

    # The dispatcher gates /resume on has_pending_interrupt (dispatcher.py:691):
    # for a DURABLE store it must see the pause on the fresh instance, else it
    # would wrongly raise OrphanedGateError and refuse a resumable gate.
    assert await rt.has_pending_interrupt(parent_b.runnable._compiled_graph, "dur-1") is True

    resumed = await rt.resume(
        parent_b.runnable._compiled_graph, {"approved": True, "approver": "x"},
        run_id="dur-1",
    )
    assert resumed.get("parent_ran") is True, (
        "durable composed-subgraph gate did not resume on a fresh instance — "
        "the #532 gate-wiring bug is real, not just the memory-checkpointer fallback"
    )
