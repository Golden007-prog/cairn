"""P1: invoke_operator refuses to dispatch a gated op without an approval (ADR-0034).

Hermetic — the gate resolver is monkeypatched, so no registry/policy/profile is
needed; this tests only the chokepoint behavior.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

import cairn.core.approval.enforce as enforce
from cairn.core.approval.enforce import APPROVALS_STATE_KEY, NeedsApprovalError
from cairn.core.approval.resolver import ApprovalGate
from cairn.core.operator import Operation, OperationContext, register_operator
from cairn.core.operator.binding import OperationBinding
from cairn.core.operator.runtime import invoke_operator


class _In(BaseModel):
    pass


class _Out(BaseModel):
    ok: bool = True


@register_operator(name="test/gated_op", version="0.1.0", capabilities=[])
class _GatedOp(Operation):
    input_schema = _In
    output_schema = _Out

    async def run(self, inp, ctx: OperationContext) -> _Out:
        return _Out(ok=True)


_BINDING = OperationBinding(operator="test/gated_op", input={}, output={"ok": "$state.gated_ok"})


async def _call(state, **kw):
    return await invoke_operator(
        _BINDING, _GatedOp, state=state, pipeline_id="run-1", node_name="n", **kw
    )


def _gate(required, **kw):
    return lambda op, b, *, state: ApprovalGate(required=required, **kw)


async def test_refuses_gated_op_without_approval(monkeypatch):
    monkeypatch.setattr(enforce, "resolve_runtime_gate", _gate(True, sources=("operator",)))
    with pytest.raises(NeedsApprovalError) as ei:
        await _call({})
    assert ei.value.request.operator == "test/gated_op"
    assert ei.value.request.sources == ("operator",)


async def test_runs_with_recorded_approval(monkeypatch):
    monkeypatch.setattr(enforce, "resolve_runtime_gate", _gate(True, sources=("operator",)))
    patch = await _call({APPROVALS_STATE_KEY: {"n": {"approved": True}}})
    assert patch == {"gated_ok": True}


async def test_ungated_op_runs_normally(monkeypatch):
    monkeypatch.setattr(enforce, "resolve_runtime_gate", _gate(False))
    assert await _call({}) == {"gated_ok": True}


async def test_mock_mode_skips_the_gate(monkeypatch):
    # Even a required gate is skipped in mock mode (dry/offline runs).
    monkeypatch.setattr(enforce, "resolve_runtime_gate", _gate(True, sources=("operator",)))
    assert await _call({}, is_mocked=True) == {"gated_ok": True}
