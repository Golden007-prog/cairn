"""Dataset row readers, resolved through an entry-point group (ADR-0084 D1).

Core never imports an operator pack — the dependency runs pack -> core, and
``tests/core/test_engine_registry.py::test_core_never_imports_an_operator_pack``
walks the AST to keep it that way. The dataset-readiness pack's row reader
(``operator_dataset_checks._io.iter_rows``: JSON/JSONL/CSV/Parquet, Hub ids,
s3) is what the Add-dataset preview (#2022) wants, so the pack **exports** it
under :data:`ENTRY_POINT_GROUP` and core loads it by name here, the way
:mod:`cairn.core.dataflow.engine_registry` loads an engine.

Without that pack installed, :func:`iter_rows` falls back to core's own
control-side reader, :func:`cairn.core.dataset_roles.read_rows` — JSONL on a
local path or s3 only — and says so when asked for a format it cannot read.
"""
from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, Callable, Iterator, Optional

from cairn.core.dataset_roles import read_rows

ENTRY_POINT_GROUP = "cairn.dataset_readers"
#: The one reader name today: ``rows`` — iterate a source's rows as dicts.
ROWS = "rows"

RowReader = Callable[..., Iterator[dict[str, Any]]]


def installed_readers() -> list[str]:
    """Reader names any installed pack registers, sorted; empty is a legitimate
    state (core's own JSONL reader still serves)."""
    return sorted({ep.name.lower() for ep in entry_points(group=ENTRY_POINT_GROUP)})


def load_row_reader(name: str = ROWS) -> Optional[RowReader]:
    """The registered reader for ``name``, or ``None`` when no pack provides it.
    ``ep.load()`` is what pulls the pack (and its optional extras) into the
    process — only when a reader is actually asked for."""
    wanted = (name or "").strip().lower()
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        if ep.name.lower() == wanted:
            return ep.load()
    return None


def iter_rows(source: str) -> Iterator[dict[str, Any]]:
    """Rows of ``source`` as dicts: through the installed pack reader when one is
    registered, else core's JSONL/s3 reader. Raises on anything unreadable —
    the caller decides what "could not read" means, never an empty preview."""
    reader = load_row_reader()
    if reader is not None:
        yield from reader(source)
        return
    try:
        rows = read_rows(source)
    except ValueError as e:
        raise ValueError(
            f"{e} — install the dataset-readiness operator pack "
            f"(cairn-operator-dataset-checks) to read CSV, Parquet or Hugging Face Hub sources"
        ) from e
    yield from rows


__all__ = ["ENTRY_POINT_GROUP", "ROWS", "RowReader", "installed_readers", "iter_rows", "load_row_reader"]
