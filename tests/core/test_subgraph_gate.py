"""#104 — a HITL gate INSIDE a child subgraph must pause the PARENT run and
resume under the parent run_id.

Before the fix the child ran as a separate invoke with its OWN checkpointer +
thread, so its interrupt() was swallowed and the parent sailed past the gate
(verified against langgraph 1.2.2). The fix: propagate has_gate child->parent
(so the parent compiles WITH a checkpointer) and embed a checkpointer-less copy
of the child, invoked inheriting the parent's config — the LangGraph-blessed
"subgraph invoked within a node" pattern.

Ungated subgraphs take the unchanged path (covered by test_subgraph_mapping /
test_pack_composition); this file pins the gated case.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest
from pydantic import BaseModel

from cairn.core.engine.compiler import compile_template
from cairn.core.operator import Operation, OperationContext, default_registry, register_operator
from cairn.core.pack.manifest import OperatorEntry
from cairn.core.runtime import InProcessRuntime


class _In(BaseModel):
    pass


class _Out(BaseModel):
    did_run: bool = True


@register_operator(name="test/subgraph_gate_op", version="0.1.0", capabilities=["test.sg"])
class _GateOp(Operation):
    input_schema = _In
    output_schema = _Out

    async def run(self, inp, ctx: OperationContext) -> _Out:
        return _Out(did_run=True)


def _ensure_op():
    if not default_registry.has("test/subgraph_gate_op"):
        default_registry.register(_GateOp)
    default_registry.attach_metadata(
        OperatorEntry(
            name="test/subgraph_gate_op",
            requires_approval=True,
            provides_capabilities=["test.sg"],
        )
    )


def _build_pack(tmp_path):
    """A two-template pack: a gated child + a parent that composes it."""
    (tmp_path / "templates").mkdir()
    (tmp_path / "operations").mkdir()
    (tmp_path / "operations" / "gate-op.yaml").write_text(
        "operator: test/subgraph_gate_op\n"
        "input: {}\n"
        "output:\n"
        "  did_run: $state.ran\n"
    )
    (tmp_path / "templates" / "childgated.md").write_text(
        "---\n"
        "name: child-gated\n"
        "version: 0.1.0\n"
        "flow:\n"
        "  type: state_machine\n"
        "  source: behavior_tree\n"
        "  root: { type: operation, ref: gate-op }\n"
        "  nodes: {}\n"
        "tools:\n"
        "  required: []\n"
        "---\n"
        "# child\n"
    )
    (tmp_path / "templates" / "parentgated.md").write_text(
        "---\n"
        "name: parent-gated\n"
        "version: 0.1.0\n"
        "flow:\n"
        "  type: state_machine\n"
        "  source: behavior_tree\n"
        "  root:\n"
        "    type: sequence\n"
        "    children:\n"
        "      - type: subgraph_node\n"
        "        template: childgated\n"
        "        inputs: {}\n"
        "        outputs:\n"
        "          ran: parent_ran\n"
        "  nodes: {}\n"
        "tools:\n"
        "  required: []\n"
        "---\n"
        "# parent\n"
    )
    return tmp_path / "templates" / "parentgated.md"


@pytest.fixture
def patched_resolver(monkeypatch, tmp_path):
    """Resolve `template: <name>` to our tmp pack's templates/<name>.md."""
    from cairn.core.engine import skill_engine

    orig = skill_engine.resolve_template_path

    def _resolve(name, *, from_pack=None):
        m = tmp_path / "templates" / f"{name}.md"
        return m if m.exists() else orig(name, from_pack=from_pack)

    monkeypatch.setattr(skill_engine, "resolve_template_path", _resolve)
    return tmp_path


async def test_subgraph_gate_pauses_parent_then_resumes(patched_resolver, tmp_path):
    _ensure_op()
    parent = compile_template(_build_pack(tmp_path))

    # has_gate propagated up: the parent compiles WITH a checkpointer even though
    # none of ITS own ops are gated — the gate lives in the child subgraph.
    assert parent.runnable._compiled_graph.checkpointer is not None

    # The parent run pauses at the CHILD's gate (interrupt propagated up) and
    # the gated op has NOT run yet.
    result = await parent.run({"run_id": "p-1"})
    assert "__interrupt__" in result
    assert result.get("parent_ran") is None

    # Resume the PARENT by its run_id → the child resumes past its gate, the
    # gated op runs, and its output maps back out (ran -> parent_ran).
    resumed = await InProcessRuntime().resume(
        parent.runnable._compiled_graph,
        {"approved": True, "approver": "x"},
        run_id="p-1",
    )
    assert resumed.get("parent_ran") is True


async def test_subgraph_gate_approval_carries_parent_run_id(patched_resolver, tmp_path):
    """#569 — the ApprovalRequest raised by a gate INSIDE the composed subgraph
    must carry the PARENT run_id, not "" (the child seed default). Before the fix
    the child state had no run_id, so audit/console correlation and the resume
    routing keyed on an empty id."""
    _ensure_op()
    parent = compile_template(_build_pack(tmp_path))
    result = await parent.run({"run_id": "p-rid-1"})
    assert "__interrupt__" in result
    intr = result["__interrupt__"]
    items = list(intr) if isinstance(intr, (list, tuple)) else [intr]
    payload = getattr(items[0], "value", None)
    assert isinstance(payload, dict), f"unexpected interrupt payload: {payload!r}"
    assert payload.get("run_id") == "p-rid-1", (
        f"subgraph gate approval carried run_id={payload.get('run_id')!r} — "
        "the parent run identity was not threaded into the child (#569)"
    )


async def test_subgraph_gate_rejection_skips_the_op(patched_resolver, tmp_path):
    _ensure_op()
    parent = compile_template(_build_pack(tmp_path))
    await parent.run({"run_id": "p-2"})  # pause at the child gate

    resumed = await InProcessRuntime().resume(
        parent.runnable._compiled_graph,
        {"approved": False, "approver": "x"},
        run_id="p-2",
    )
    # Rejected → the gated op was skipped, so its output never mapped out.
    assert resumed.get("parent_ran") is None
