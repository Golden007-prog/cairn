"""#569/#581 slice 2 — the flatten graph-splice is EQUIVALENT to the nested
subgraph_node for a gated cross-pack subgraph, in-process.

With OBS_FLATTEN_COMPOSITION=1 a gated child's nodes are spliced into the parent
graph as top-level nodes. The child's approval gate must still pause the parent
run and resume under the parent run_id — same observable behavior as the nested
path (test_subgraph_gate) — and the child's outputs must still map up. That
equivalence is the safety gate before flatten becomes the default (which is what
fixes the #569 Temporal loop: a top-level gate interrupt reaches the workflow
signal loop instead of looping inside one activity).
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


@register_operator(name="test/flatten_gate_op", version="0.1.0", capabilities=["test.fg"])
class _GateOp(Operation):
    input_schema = _In
    output_schema = _Out

    async def run(self, inp, ctx: OperationContext) -> _Out:
        return _Out(did_run=True)


def _ensure_op():
    if not default_registry.has("test/flatten_gate_op"):
        default_registry.register(_GateOp)
    default_registry.attach_metadata(
        OperatorEntry(
            name="test/flatten_gate_op",
            requires_approval=True,
            provides_capabilities=["test.fg"],
        )
    )


def _build_pack(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "operations").mkdir()
    (tmp_path / "operations" / "gate-op.yaml").write_text(
        "operator: test/flatten_gate_op\n"
        "input: {}\n"
        "output:\n"
        "  did_run: $state.ran\n"      # a TOP-LEVEL child key (dropped if schema misses it)
    )
    (tmp_path / "templates" / "childgated.md").write_text(
        "---\nname: child-fg\nversion: 0.1.0\n"
        "flow:\n  type: state_machine\n  source: behavior_tree\n"
        "  root: { type: operation, ref: gate-op }\n  nodes: {}\n"
        "tools:\n  required: []\n---\n# child\n"
    )
    (tmp_path / "templates" / "parentgated.md").write_text(
        "---\nname: parent-fg\nversion: 0.1.0\n"
        "flow:\n  type: state_machine\n  source: behavior_tree\n"
        "  root:\n    type: sequence\n    children:\n"
        "      - type: subgraph_node\n        template: childgated\n"
        "        inputs: {}\n        outputs:\n          ran: parent_ran\n"
        "  nodes: {}\ntools:\n  required: []\n---\n# parent\n"
    )
    return tmp_path / "templates" / "parentgated.md"


@pytest.fixture
def patched_resolver(monkeypatch, tmp_path):
    from cairn.core.engine import skill_engine

    orig = skill_engine.resolve_template_path

    def _resolve(name, *, from_pack=None):
        m = tmp_path / "templates" / f"{name}.md"
        return m if m.exists() else orig(name, from_pack=from_pack)

    monkeypatch.setattr(skill_engine, "resolve_template_path", _resolve)
    return tmp_path


async def _run_and_resume(parent):
    """Run to the gate (interrupt), then resume approved — return final state."""
    result = await parent.run({"run_id": "p-flat"})
    assert "__interrupt__" in result, "gated child did not pause the parent run"
    return await InProcessRuntime().resume(
        parent.runnable._compiled_graph,
        {"approved": True, "approver": "x"},
        run_id="p-flat",
    )


async def test_flatten_gated_subgraph_pauses_then_resumes(patched_resolver, tmp_path, monkeypatch):
    monkeypatch.setenv("OBS_FLATTEN_COMPOSITION", "1")
    _ensure_op()
    parent = compile_template(_build_pack(tmp_path))
    # has_gate propagated → parent compiled WITH a checkpointer (needed to resume).
    assert parent.runnable._compiled_graph.checkpointer is not None
    # flatten defers output mapping until the gate approves (correct: the exit
    # adapter runs after the child's gate node).
    paused = await parent.run({"run_id": "p-flat"})
    assert "__interrupt__" in paused and paused.get("parent_ran") is None
    resumed = await InProcessRuntime().resume(
        parent.runnable._compiled_graph, {"approved": True, "approver": "x"}, run_id="p-flat",
    )
    # the flattened child's gate ran + its top-level output mapped up (ran->parent_ran)
    assert resumed.get("parent_ran") is True


async def test_flatten_matches_nested_result(patched_resolver, tmp_path, monkeypatch):
    """Same pack, flag OFF (nested) vs ON (flattened) → identical parent_ran."""
    _ensure_op()
    src = _build_pack(tmp_path)

    monkeypatch.delenv("OBS_FLATTEN_COMPOSITION", raising=False)
    nested = await _run_and_resume(compile_template(src))

    monkeypatch.setenv("OBS_FLATTEN_COMPOSITION", "1")
    flat = await _run_and_resume(compile_template(src))

    assert nested.get("parent_ran") == flat.get("parent_ran") == True  # noqa: E712


async def test_a_flattened_opted_in_child_node_runs_under_its_recorded_contract(patched_resolver, tmp_path, monkeypatch):
    """#2241 C (D2, flattened arm) — the child's node is spliced into the parent under its
    bare ref; the compile factory carries `<child template>/` so the node reads the key the
    launch door recorded, and a refusal fails the run after the gate resumes."""
    from cairn.core.operator.effective import ContractConformanceError

    monkeypatch.setenv("OBS_FLATTEN_COMPOSITION", "1")
    _ensure_op()
    monkeypatch.setattr(_GateOp, "implements", ("contract://test.fg/1.0.0",), raising=False)
    envelope = {"capability_ref": "contract://test.fg/1.0.0", "operator_ref": "test/flatten_gate_op",
                "operator_version": str(_GateOp.version), "refs": {"input": "contract://i/1.0.0", "configuration": "contract://c/1.0.0", "output": "contract://o/1.0.0"},
                "schemas": {"contract://i/1.0.0": {}, "contract://c/1.0.0": {}, "contract://o/1.0.0": {"type": "object", "required": ["never"]}}}
    parent = compile_template(_build_pack(tmp_path))
    seeded = {"run_id": "p-flat-c", "__contract_invocations__": {"__leg__": "L", "childgated/gate-op": envelope}}
    result = await parent.run(seeded)
    assert "__interrupt__" in result, "gated child did not pause the parent run"
    with pytest.raises(ContractConformanceError, match="output"):
        await InProcessRuntime().resume(parent.runnable._compiled_graph, {"approved": True, "approver": "x"}, run_id="p-flat-c")


@register_operator(name="test/flatten_plain_op", version="0.1.0", capabilities=["test.fp"], implements=["contract://test.fp/1.0.0"])
class _PlainOp(Operation):
    input_schema = _In
    output_schema = _Out

    async def run(self, inp, ctx: OperationContext) -> _Out:
        return _Out(did_run=True)


async def test_an_ungated_subgraph_inside_a_flattened_child_keeps_the_ancestor_prefix(patched_resolver, tmp_path, monkeypatch):
    """#2241 C review R5 — a flattened child's own nested (ungated) subgraph seeds its
    child from the parent channel under `<child>/<grandchild>/…`, the key the door recorded."""
    from cairn.core.operator.effective import ContractConformanceError

    monkeypatch.setenv("OBS_FLATTEN_COMPOSITION", "1")
    _ensure_op()
    if not default_registry.has("test/flatten_plain_op"):
        default_registry.register(_PlainOp)
    _build_pack(tmp_path)
    (tmp_path / "operations" / "plain-op.yaml").write_text("operator: test/flatten_plain_op\ninput: {}\noutput:\n  did_run: $state.ran\n")
    (tmp_path / "templates" / "plain.md").write_text(
        "---\nname: plain\nversion: 0.1.0\nflow:\n  type: state_machine\n  source: behavior_tree\n"
        "  root: { type: operation, ref: plain-op }\n  nodes: {}\ntools:\n  required: []\n---\n# plain\n")
    (tmp_path / "templates" / "childgated.md").write_text(
        "---\nname: child-fg\nversion: 0.1.0\nflow:\n  type: state_machine\n  source: behavior_tree\n"
        "  root:\n    type: sequence\n    children:\n      - { type: operation, ref: gate-op }\n"
        "      - { type: subgraph_node, template: plain }\n  nodes: {}\ntools:\n  required: []\n---\n# child\n")
    envelope = {"capability_ref": "contract://test.fp/1.0.0", "operator_ref": "test/flatten_plain_op", "operator_version": "0.1.0",
                "refs": {"input": "contract://i/1.0.0", "configuration": "contract://c/1.0.0", "output": "contract://o/1.0.0"},
                "schemas": {"contract://i/1.0.0": {}, "contract://c/1.0.0": {}, "contract://o/1.0.0": {"type": "object", "required": ["never"]}}}
    parent = compile_template(tmp_path / "templates" / "parentgated.md")
    result = await parent.run({"run_id": "p-flat-r5", "__contract_invocations__": {"__leg__": "L", "childgated/plain/plain-op": envelope}})
    assert "__interrupt__" in result
    with pytest.raises(ContractConformanceError, match="output"):      # the grandchild got its entry (a lost one would say "no entry")
        await InProcessRuntime().resume(parent.runnable._compiled_graph, {"approved": True, "approver": "x"}, run_id="p-flat-r5")
