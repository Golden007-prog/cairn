"""Inline operation bindings — #1107 phase 2a, pack-redesign §3.

The contract these tests pin:

  1. An operation node carries EXACTLY ONE of ``ref`` | ``operator``.
  2. Every consumer that used to read only the binding FILE sees the inline
     form too (channel scan, the full binding surface on the node).
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from cairn.core.engine.schema import OperationBTNode
from cairn.core.operator import Operation, OperationContext
from cairn.core.operator import default_registry as op_registry


# ── the exactly-one-of rule ──────────────────────────────────────────────────

def test_a_node_with_BOTH_ref_and_operator_is_rejected():
    with pytest.raises(ValidationError, match="exactly ONE"):
        OperationBTNode.model_validate(
            {"type": "operation", "ref": "run-x", "operator": "cost/gate"}
        )


def test_a_node_with_NEITHER_is_rejected():
    with pytest.raises(ValidationError, match="exactly ONE"):
        OperationBTNode.model_validate({"type": "operation"})


def test_inline_fields_on_a_ref_node_are_rejected():
    """A ref node smuggling an input: map would silently diverge from its
    file — the two sources of truth problem this whole issue exists to end."""
    with pytest.raises(ValidationError, match="inline binding fields"):
        OperationBTNode.model_validate(
            {"type": "operation", "ref": "run-x", "input": {"a": 1}}
        )


def test_node_ref_naming():
    inline = OperationBTNode.model_validate(
        {"type": "operation", "operator": "cost/gate"}
    )
    assert inline.node_ref == "cost_gate"          # derived
    pinned = OperationBTNode.model_validate(
        {"type": "operation", "operator": "cost/gate", "id": "run-cost-gate"}
    )
    assert pinned.node_ref == "run-cost-gate"      # codemod pins the old name


# ── fixture: an inline-bound operator ────────────────────────────────────────────

class _EchoIn(BaseModel):
    message: str


class _EchoOut(BaseModel):
    echoed: str


@pytest.fixture
def echo_operator():
    snapshot = op_registry.all()

    class EchoOp(Operation[_EchoIn, _EchoOut]):
        name = "test/echo"
        version = "0.1.0"
        input_schema = _EchoIn
        output_schema = _EchoOut

        def run(self, inp, ctx: OperationContext):
            return _EchoOut(echoed=f"ECHO:{inp.message}")

    op_registry.register(EchoOp)
    try:
        yield EchoOp
    finally:
        op_registry.clear()
        for name, cls in snapshot.items():
            op_registry.register(cls)


# ── the consumers that used to read only the file ────────────────────────────

def test_channel_scan_sees_inline_state_refs(echo_operator):
    """§24.22: LangGraph drops undeclared channels — if the channel scan
    missed inline nodes, every inlined output would silently vanish."""
    from cairn.core.engine.compiler import _binding_state_keys

    node = OperationBTNode.model_validate({
        "type": "operation", "operator": "test/echo",
        "input": {"message": "$state.symptom"},
        "output": {"echoed": "$state.final_output"},
    })
    keys = _binding_state_keys(node.inline_binding())
    assert {"symptom", "final_output"} <= keys


def test_inline_binding_roundtrips_every_optional_field():
    """The 10-field §24.20.5 surface survives the lift — secrets, execution,
    approval… — or an inline node would silently drop governance metadata."""
    node = OperationBTNode.model_validate({
        "type": "operation", "operator": "train/stub", "id": "run-train",
        "version": ">= 0.1",
        "input": {"dataset_uri": "$input.dataset_path"},
        "output": {"status": "$state.findings.train_status"},
        "secrets": ["HF_TOKEN"],
        "approval": {"required": True},
    })
    b = node.inline_binding()
    assert b.operator == "train/stub"
    assert b.version == ">= 0.1"
    assert b.secrets == ["HF_TOKEN"]
    assert b.approval is not None and b.approval.required is True
