"""Typed artifacts and the compile-time wire check (#1137, ADR-0084).

A pipeline wires one leg's output into the next leg's input. Until now those
wires were untyped strings, so `report → dataset` was indistinguishable from
`dataset → dataset` and only failed once the second leg was already running —
after the first leg had spent its money. This makes the wire a typed edge that
is checked when the pipeline is planned, before anything is submitted.

The check is DELIBERATELY one-sided: a mismatch is an error, but an *undeclared*
kind is not. Packs adopt `produces:`/`consumes:` incrementally, and a pipeline
that declares nothing behaves exactly as it did before. Type checking that
forces a big-bang migration gets turned off; type checking that rewards each
annotation gets adopted.
"""
from __future__ import annotations

from typing import Optional

#: The initial artifact vocabulary. Deliberately small — these are the things
#: cairn's legs actually hand each other. A kind cairn cannot reason about adds
#: ceremony without adding safety.
ARTIFACT_KINDS = (
    "Dataset",      # rows, the data plane's unit
    "Model",        # trained weights / an adapter
    "Report",       # a rendered human-facing artifact
    "VectorIndex",  # an embedded, queryable corpus
    "Checkpoint",   # resumable mid-training state
    "Document",     # a single unstructured item
    "State",        # opaque run state handed between legs
)


class WireTypeError(ValueError):
    """A wire connects two incompatible artifact kinds."""


#: Trailing marker on a `consumes:` kind that declares the wire OPTIONAL —
#: `consumes: {report: Report?}` (#1251, ADR-0087 D2). An optional wire is the
#: ONLY way to read a leg that carries `when:`, because that leg may be routed
#: off; the key is then simply absent from the downstream leg's inputs.
OPTIONAL_SUFFIX = "?"


def is_optional(kind: Optional[str]) -> bool:
    """Does this `consumes:` declaration end in `?` (an optional wire)?

    A bare `?` is legal and means "optional, kind undeclared" — packs adopting
    optional legs before they adopt typed artifacts still get the safety the
    plan-time check exists to give.
    """
    return str(kind or "").strip().endswith(OPTIONAL_SUFFIX)


def normalize(kind: Optional[str]) -> str:
    """Canonical kind name, or '' when unknown/undeclared.

    Case-insensitive on purpose: `dataset` in hand-written YAML should not be a
    different type from `Dataset`. A trailing `?` (the optional-wire marker) is
    part of the WIRE's declaration, not of the kind, so it is stripped here —
    `Report?` and `Report` are the same artifact kind.
    """
    if not kind:
        return ""
    want = str(kind).strip().rstrip(OPTIONAL_SUFFIX).strip().lower()
    for k in ARTIFACT_KINDS:
        if k.lower() == want:
            return k
    return ""


def compatible(produced: str, consumed: str) -> bool:
    """May a `produced` artifact feed a `consumed` slot?

    Unknown on either side means "not enough information to object" — see the
    module docstring on incremental adoption. `State` is the deliberate escape
    hatch: it is opaque by definition, so it neither constrains nor is
    constrained.
    """
    p, c = normalize(produced), normalize(consumed)
    if not p or not c:
        return True
    if p == "State" or c == "State":
        return True
    return p == c


def check_wire(
    *,
    src_step: str, src_field: str, produced: str,
    dst_step: str, dst_input: str, consumed: str,
) -> None:
    """Raise :class:`WireTypeError` naming BOTH ends, or return.

    Naming both ends is the whole point: "expected Dataset" tells you what broke,
    "`fix.report` (Report) → `train.dataset` (Dataset)" tells you where to fix it.
    """
    if compatible(produced, consumed):
        return
    raise WireTypeError(
        f"wire {src_step}.{src_field} → {dst_step}.{dst_input} connects "
        f"{normalize(produced)} to {normalize(consumed)}: a "
        f"{normalize(produced)} cannot be consumed as a {normalize(consumed)}. "
        f"Fix the wire, or declare the real kinds on both legs."
    )


__all__ = [
    "ARTIFACT_KINDS", "OPTIONAL_SUFFIX", "WireTypeError", "check_wire",
    "compatible", "is_optional", "normalize",
]
