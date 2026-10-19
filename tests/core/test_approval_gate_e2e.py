"""P2: a gated operation pauses the template run via interrupt() and resumes
after approval (ADR-0034). Proves the compiler attaches a checkpointer + injects
the gate, and the in-process runtime pauses + resumes.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from pydantic import BaseModel

from cairn.core.engine.compiler import compile_template
from cairn.core.operator import Operation, OperationContext, default_registry, register_operator
from cairn.core.pack.manifest import OperatorEntry
from cairn.core.runtime import InProcessRuntime


class _GIn(BaseModel):
    pass


class _GOut(BaseModel):
    did_run: bool = True


@register_operator(name="test/needs_approval", version="0.1.0", capabilities=["test.x"])
class _NeedsApprovalOp(Operation):
    input_schema = _GIn
    output_schema = _GOut

    async def run(self, inp, ctx: OperationContext) -> _GOut:
        return _GOut(did_run=True)


@register_operator(name="test/free_op", version="0.1.0", capabilities=["test.y"])
class _FreeOp(Operation):
    input_schema = _GIn
    output_schema = _GOut

    async def run(self, inp, ctx: OperationContext) -> _GOut:
        return _GOut(did_run=True)


def _ensure(op_cls, name, *, requires_approval):
    if not default_registry.has(name):
        default_registry.register(op_cls)
    default_registry.attach_metadata(
        OperatorEntry(name=name, requires_approval=requires_approval,
                      provides_capabilities=["test.x" if requires_approval else "test.y"])
    )


def _pack(tmp_path: Path, *, name: str, ref: str, operator: str) -> Path:
    (tmp_path / "templates").mkdir()
    (tmp_path / "operations").mkdir()
    (tmp_path / "operations" / f"{ref}.yaml").write_text(textwrap.dedent(f"""\
        operator: {operator}
        input: {{}}
        output:
          did_run: $state.findings.gated_did_run
    """))
    tmpl = tmp_path / "templates" / "check.md"
    tmpl.write_text(textwrap.dedent(f"""\
        ---
        name: {name}
        version: 0.1.0
        flow:
          type: state_machine
          source: behavior_tree
          root: {{ type: operation, ref: {ref} }}
          nodes: {{}}
        tools:
          required: []
        ---
        # gated
    """))
    return tmpl


async def test_gated_template_pauses_then_resumes_on_approval(tmp_path):
    _ensure(_NeedsApprovalOp, "test/needs_approval", requires_approval=True)
    compiled = compile_template(
        _pack(tmp_path, name="gated-approve", ref="run-x", operator="test/needs_approval")
    )

    # The gated graph gets a checkpointer (durable pause).
    assert compiled.runnable._compiled_graph.checkpointer is not None

    # Run pauses at the approval interrupt — the operator did NOT run yet.
    result = await compiled.run({})
    assert "__interrupt__" in result
    assert result.get("findings", {}).get("gated_did_run") is None

    # Resume with an approval → the operator runs.
    rt = InProcessRuntime()
    resumed = await rt.resume(
        compiled.runnable._compiled_graph, {"approved": True, "approver": "alice"},
        run_id="gated-approve",
    )
    assert resumed.get("findings", {}).get("gated_did_run") is True


async def test_gated_template_rejection_skips_the_operator(tmp_path):
    _ensure(_NeedsApprovalOp, "test/needs_approval", requires_approval=True)
    compiled = compile_template(
        _pack(tmp_path, name="gated-reject", ref="run-x", operator="test/needs_approval")
    )
    await compiled.run({})  # pause

    rt = InProcessRuntime()
    resumed = await rt.resume(
        compiled.runnable._compiled_graph, {"approved": False, "approver": "alice"},
        run_id="gated-reject",
    )
    # Operator skipped; a rejection marker recorded, no gated_did_run.
    assert resumed.get("findings", {}).get("gated_did_run") is None
    assert resumed.get("last_action_status") == "rejected"


async def test_ungated_template_has_no_checkpointer_and_runs(tmp_path):
    _ensure(_FreeOp, "test/free_op", requires_approval=False)
    compiled = compile_template(
        _pack(tmp_path, name="ungated-t", ref="run-y", operator="test/free_op")
    )
    assert compiled.runnable._compiled_graph.checkpointer is None
    result = await compiled.run({})
    assert "__interrupt__" not in result
    assert result.get("findings", {}).get("gated_did_run") is True


async def test_run_id_threads_through_for_resume_and_isolation(tmp_path):
    """The input run_id becomes the graph thread_id (an internal issue), so a
    paused run is resumable by THAT id — and two runs of the same template on
    distinct run_ids are isolated (the fix for the old thread_id=spec.name
    collision the dispatcher relied on)."""
    _ensure(_NeedsApprovalOp, "test/needs_approval", requires_approval=True)
    compiled = compile_template(
        _pack(tmp_path, name="gated-rid", ref="run-x", operator="test/needs_approval")
    )
    graph = compiled.runnable._compiled_graph
    rt = InProcessRuntime()

    # Two runs of the SAME template on distinct run_ids → both pause independently.
    a = await compiled.run({"run_id": "run-A"})
    b = await compiled.run({"run_id": "run-B"})
    assert "__interrupt__" in a and "__interrupt__" in b

    # Resume ONLY run-A by its id → it completes.
    resumed_a = await rt.resume(graph, {"approved": True, "approver": "x"}, run_id="run-A")
    assert resumed_a.get("findings", {}).get("gated_did_run") is True

    # run-B is untouched — still paused on its own thread (isolation, not a
    # shared spec.name thread that resuming A would have cleared).
    state_b = graph.get_state({"configurable": {"thread_id": "run-B"}})
    assert state_b.next, "run-B should still be paused (isolated from run-A)"
