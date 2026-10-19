"""#1107 phase 2b: compile-time elaboration of `with:`-form inline bindings.

The `uses:`/`with:` model (GitHub Actions proved it scales): a call site names
the operator and passes ONLY what deviates; the operator's own input schema is
the contract. Elaboration runs at COMPILE time and produces a plain, fully
explicit :class:`OperationBinding` — so the runtime, the audit surface, and
the §24.22 channel scan see exactly what a hand-written binding would say.
Nothing is resolved late; the sugar is spelling, not semantics.

Rules (per the #1107 amendment + the 2026-07-30 rulings):

- explicit ``input:`` → NO elaboration (2a strict form, byte-compatible);
- ``with:`` (or a bare ``operator:`` with neither map) → for every
  input-schema field not mapped:
    * required  → ``$input.<field>``          (absent at run = loud failure)
    * optional  → ``$default($input.<field>, <schema default>)`` — literally
      the line pack authors write by hand today, generated instead;
- missing ``output:`` → every output-schema field lands producer-named at
  ``$state.<node_id>.<field>`` (consumers read ``node.field``);
- computed fields stay in python_nodes — elaboration never invents logic.
"""
from __future__ import annotations

import json
from typing import Any, Type

from cairn.core.operator.binding import OperationBinding
from cairn.core.operator.protocol import Operation

__all__ = ["elaborate_binding"]


def _default_literal(value: Any) -> str:
    """Render a schema default as the ``$default`` literal arg (JSON spelling —
    the grammar accepts JSON + Python; JSON is what pack YAML reads like)."""
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)


def elaborate_binding(
    binding: OperationBinding,
    operator_cls: Type[Operation],
    *,
    node_ref: str,
    auto_input: bool,
    auto_output: bool,
) -> OperationBinding:
    """Fill unmapped input fields by name and unmapped outputs producer-named.

    ``auto_input``/``auto_output`` come from the node's spelling: `with:` (or
    bare operator) opts in; explicit `input:`/`output:` maps opt out — the 2a
    strict form must stay byte-compatible.
    """
    inp = dict(binding.input)
    out = dict(binding.output)

    in_schema = getattr(operator_cls, "input_schema", None)
    if auto_input and in_schema is not None:
        for field, info in in_schema.model_fields.items():
            if field in inp:
                continue
            if info.is_required():
                inp[field] = f"$input.{field}"
            else:
                inp[field] = (
                    f"$default($input.{field}, {_default_literal(info.default)})"
                )

    out_schema = getattr(operator_cls, "output_schema", None)
    if auto_output and not out and out_schema is not None:
        for field in out_schema.model_fields:
            out[field] = f"$state.{node_ref}.{field}"

    return binding.model_copy(update={"input": inp, "output": out})
