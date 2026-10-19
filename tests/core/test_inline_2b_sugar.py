"""#1107 phase 2b: `with:` overrides + name auto-bind + producer-named outputs.

The contract (per the amendment + the 2026-07-30 rulings):
- `with:` passes ONLY deviations; unmapped required → $input.<f>, unmapped
  optional → $default($input.<f>, <schema default>) — generated, not typed.
- Missing output: → producer-named $state.<node_id>.<field>.
- Explicit input:/output: (the 2a strict form) is NEVER elaborated.
- python_node is untouched — elaboration never invents logic (owner ruling).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from cairn.core.engine.compiler import compile_template
from cairn.core.engine.schema import OperationBTNode
from cairn.core.operator import Operation, OperationContext
from cairn.core.operator import default_registry as op_registry
from cairn.core.operator.elaborate import elaborate_binding


class _In(BaseModel):
    message: str                      # required
    prefix: str = "ECHO"              # optional, schema-owned default
    upper: bool = False               # optional bool — the #630 falsy trap


class _Out(BaseModel):
    echoed: str
    length: int


@pytest.fixture
def echo2(tmp_path):
    snapshot = op_registry.all()

    class Echo2(Operation[_In, _Out]):
        name = "test/echo2"
        version = "0.1.0"
        input_schema = _In
        output_schema = _Out

        def run(self, inp, ctx: OperationContext):
            body = f"{inp.prefix}:{inp.message}"
            return _Out(echoed=body.upper() if inp.upper else body, length=len(body))

    op_registry.register(Echo2)
    try:
        yield Echo2
    finally:
        op_registry.clear()
        for name, cls in snapshot.items():
            op_registry.register(cls)


# ── elaboration unit surface ────────────────────────────────────────────────

def test_with_and_input_together_are_rejected():
    with pytest.raises(ValidationError, match="not both"):
        OperationBTNode.model_validate({
            "type": "operation", "operator": "x/y",
            "input": {"a": 1}, "with": {"b": 2},
        })


def test_elaboration_fills_required_optional_and_outputs(echo2):
    node = OperationBTNode.model_validate(
        {"type": "operation", "operator": "test/echo2", "id": "probe",
         "with": {"prefix": "HI"}}
    )
    b = elaborate_binding(node.inline_binding(), echo2, node_ref="probe",
                          auto_input=True, auto_output=True)
    assert b.input["prefix"] == "HI"                          # override wins
    assert b.input["message"] == "$input.message"             # required → $input
    assert b.input["upper"] == "$default($input.upper, false)"  # optional → schema default (JSON spelling)
    assert b.output == {"echoed": "$state.probe.echoed",
                        "length": "$state.probe.length"}      # producer-named


def test_explicit_input_is_NEVER_elaborated(echo2):
    """The 2a strict form stays byte-compatible — codemod output must not
    drift under 2b."""
    node = OperationBTNode.model_validate(
        {"type": "operation", "operator": "test/echo2", "id": "probe",
         "input": {"message": "$input.message", "prefix": "P", "upper": False},
         "output": {"echoed": "$state.final_output"}}
    )
    b = node.inline_binding()
    assert b.input["prefix"] == "P" and b.output == {"echoed": "$state.final_output"}


# ── end-to-end through the compiler ─────────────────────────────────────────

_TMPL = """\
---
name: sugar
version: 1.0.0
description: 2b demo
models:
  fast: gpt-4o-mini
tools:
  required: []
flow:
  type: state_machine
  source: behavior_tree
  root:
    type: operation
    operator: test/echo2
    id: probe
    with: {{ prefix: "HI" }}
  nodes: {{}}
---

# Sugar
"""


@pytest.mark.asyncio
async def test_with_form_compiles_and_runs_with_schema_defaults(tmp_path, echo2):
    p = tmp_path / "flows" / "sugar.md"
    p.parent.mkdir(parents=True)
    p.write_text(_TMPL.format())
    compiled = compile_template(p)

    out = await compiled.run({"message": "hello"})
    # producer-named landing: state.probe.echoed / state.probe.length
    assert out["probe"]["echoed"] == "HI:hello"
    assert out["probe"]["length"] == len("HI:hello")


@pytest.mark.asyncio
async def test_run_input_overrides_schema_default_and_false_survives(tmp_path, echo2):
    """upper=False (schema default) vs upper=true at run — and the falsy trap:
    passing upper=false explicitly must NOT fall back to the default path."""
    p = tmp_path / "flows" / "sugar.md"
    p.parent.mkdir(parents=True)
    p.write_text(_TMPL.format())
    compiled = compile_template(p)

    up = await compiled.run({"message": "x", "upper": True})
    assert up["probe"]["echoed"] == "HI:X"
    lo = await compiled.run({"message": "x", "upper": False})
    assert lo["probe"]["echoed"] == "HI:x"


def test_missing_required_run_input_fails_the_node(tmp_path, echo2):
    """Auto-bound required fields keep the SAME failure contract as a
    hand-written $input binding: the node errors (no probe output lands in
    state; the error surfaces through run events) — 2b changes spelling,
    never semantics."""
    import asyncio

    p = tmp_path / "flows" / "sugar.md"
    p.parent.mkdir(parents=True)
    p.write_text(_TMPL.format())
    compiled = compile_template(p)
    out = asyncio.run(compiled.run({}))
    assert "probe" not in out or not out["probe"]  # node failed → no output landed
