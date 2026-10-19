"""Compile-time operation binding validation (SCH4 #192).

Catches binding↔operator mismatches at compile time instead of waiting
for runtime. Three rules, mirroring the subgraph-mapping pattern in
:func:`cairn.core.engine.compiler._validate_subgraph_mapping`:

  1. **Required input fields must be bound.** If the operator's
     ``input_schema`` marks ``service`` as required and the binding
     doesn't include ``service:``, fail loud with file context.
  2. **No unknown input keys.** A binding key that doesn't exist on
     ``input_schema`` is almost certainly a typo (``servce`` for
     ``service``). Reject immediately.
  3. **No unknown output keys.** Same for ``output:`` — every key must
     name a real field on ``output_schema``.

Runtime semantics in :func:`invoke_operator_inprocess` already enforce
rule #3 (the output-field check) and full Pydantic validation of
resolved inputs. SCH4 just moves rules #1 and #2 earlier — the doctor
should be able to spot these without executing the template.

This module does NOT validate $-binding expressions themselves; that
work lives in :mod:`cairn.core.operator.runtime` (OP4 #183 grammar).
"""

from __future__ import annotations

from typing import Type

from cairn.core.engine.schemas import (
    required_fields,
    schema_fields,
)
from cairn.core.operator.binding import OperationBinding
from cairn.core.operator.protocol import Operation


class OperationBindingError(ValueError):
    """A compile-time mismatch between an operation binding and its operator.

    Error messages always carry:

    - A ``site`` describing where the binding lives (file path or BT-node
      ref) so the author can jump straight to the source.
    - The operator name + field set so they don't have to chase the
      operator class to know what's available.
    """


def validate_operation_binding(
    binding: OperationBinding,
    operator_cls: Type[Operation],
    *,
    site: str,
) -> None:
    """Check that ``binding`` lines up with the operator's schemas.

    :param binding: the parsed operation binding (:class:`OperationBinding`).
    :param operator_cls: the resolved operator class. Must have
        ``input_schema`` and ``output_schema`` class attributes (Pydantic
        models — protocol contract from §24.20.13).
    :param site: human-readable origin (e.g.,
        ``"operations/query_logs.yaml"`` or ``"OperationBTNode 'query_logs'"``)
        — surfaces in error messages.

    :raises OperationBindingError: any rule violation. Errors are
        all-at-once for input then for output (one error per call —
        first violation wins, mirroring the subgraph validator).
    """
    op_name = getattr(operator_cls, "name", operator_cls.__name__)

    # Collect ALL violations and raise once. A typo like ``servce`` for
    # ``service`` shows up as BOTH a missing required field AND an
    # unknown bound field — reporting them together makes the typo
    # obvious. (First-violation-wins, as in the subgraph validator,
    # buries that signal.)
    errors: list[str] = []

    # ── Input side ───────────────────────────────────────────────────
    in_schema = operator_cls.input_schema
    if in_schema is not None:
        bound = set(binding.input.keys())
        required = required_fields(in_schema)
        known = schema_fields(in_schema)

        missing = required - bound
        if missing:
            errors.append(
                f"omits required input field(s) {sorted(missing)}; "
                f"declared on {in_schema.__name__} (known fields: "
                f"{sorted(known)})"
            )

        unknown = bound - known
        if unknown:
            errors.append(
                f"references unknown input field(s) {sorted(unknown)} "
                f"(not declared on {in_schema.__name__}; known: "
                f"{sorted(known)})"
            )

    # ── Output side ──────────────────────────────────────────────────
    out_schema = operator_cls.output_schema
    if out_schema is not None and binding.output:
        bound = set(binding.output.keys())
        known = schema_fields(out_schema)

        unknown = bound - known
        if unknown:
            errors.append(
                f"output references unknown field(s) {sorted(unknown)} "
                f"(not declared on {out_schema.__name__}; known: "
                f"{sorted(known)})"
            )

    if errors:
        joined = "; ".join(errors)
        raise OperationBindingError(
            f"{site}: operation binding for operator {op_name!r} {joined}"
        )


__all__ = [
    "OperationBindingError",
    "validate_operation_binding",
]
