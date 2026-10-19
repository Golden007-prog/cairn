"""The operator reporting DECLARATION (ADR-0103, #2035) — types only.

Stdlib-only on purpose. ``cairn.core.pack.manifest`` reads and validates the
``[operator."<n>".reports]`` table with these types, and the manifest must
stay importable without the operator package (its ``__init__`` pulls in the
runtime, the loader and, through the loader, the manifest itself — a cycle).
The runtime half — the typed :class:`Reporter` and its sinks — lives in
:mod:`cairn.core.operator.reports`, which re-exports everything here so an
operator imports one name.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, ClassVar, Optional

# ─── run-event types this contract emits ────────────────────────────────

PROGRESS_EVENT_TYPE = "progress"      # {node, stage, percent, eta_s, cost_usd, seq}
METRICS_EVENT_TYPE = "metrics"        # {node, step, series: {name: value}, seq}
SCALARS_EVENT_TYPE = "scalars"        # {node, values: {name: value}, seq}
CHECKPOINT_EVENT_TYPE = "checkpoint"  # {node, ref, step, metric, seq}
DIVERGENCE_EVENT_TYPE = "divergence"  # {node, kind, step, value, seq}
PARAMETERS_EVENT_TYPE = "parameters"  # {node, operator, inputs, config, scopes} — written by the RUNTIME, never by an operator

REPORT_EVENT_TYPES = frozenset({
    PROGRESS_EVENT_TYPE, METRICS_EVENT_TYPE, SCALARS_EVENT_TYPE,
    CHECKPOINT_EVENT_TYPE, DIVERGENCE_EVENT_TYPE,
})

_NAME_RE = re.compile(r"^[a-z][a-z0-9_./-]*$")

STRICT_ENV = "CAIRN_REPORT_STRICT"


def strict_reports_enabled() -> bool:
    """Raise on an undeclared name (tests, ``cairn validate``) instead of dropping it."""
    return os.environ.get(STRICT_ENV, "").strip().lower() in {"1", "true", "yes"}


class UndeclaredReportError(ValueError):
    """An operator reported a stage / series / scalar its ``reports`` did not declare."""


@dataclass(frozen=True)
class Series:
    """One declared time-series: ``name`` plus a display ``unit`` (may be empty)."""

    name: str
    unit: str = ""


@dataclass(frozen=True)
class ReportSpec:
    """What an operator reports. Declared on the class as ``reports = ReportSpec(...)``.

    ``ReportSpec.NONE`` is the explicit "this operator reports nothing" — it
    must be stated; a missing attribute is a manifest-gate failure (the same
    rule as ``risk``). Names are validated at construction so a typo fails at
    import time, not at the first emit.
    """

    stages: tuple[str, ...] = ()
    series: tuple[Series, ...] = ()
    scalars: tuple[str, ...] = ()

    NONE: ClassVar["ReportSpec"]  # assigned below the class; ClassVar so the dataclass does not make it a field

    def __init__(
        self,
        stages: Optional[list[str] | tuple[str, ...]] = None,
        series: Optional[list[Series | tuple[str, str] | str] | tuple[Any, ...]] = None,
        scalars: Optional[list[str] | tuple[str, ...]] = None,
    ) -> None:
        st = tuple(str(s) for s in (stages or ()))
        se: list[Series] = []
        for item in (series or ()):
            if isinstance(item, Series):
                se.append(item)
            elif isinstance(item, str):
                se.append(Series(item, ""))
            else:
                n, u = item
                se.append(Series(str(n), str(u)))
        sc = tuple(str(s) for s in (scalars or ()))
        for group, names in (("stages", st), ("series", tuple(s.name for s in se)), ("scalars", sc)):
            bad = [n for n in names if not _NAME_RE.match(n)]
            if bad:
                raise ValueError(f"ReportSpec.{group}: invalid name(s) {bad!r} (lowercase, [a-z0-9_./-])")
            if len(set(names)) != len(names):
                raise ValueError(f"ReportSpec.{group}: duplicate name(s) in {list(names)!r}")
        object.__setattr__(self, "stages", st)
        object.__setattr__(self, "series", tuple(se))
        object.__setattr__(self, "scalars", sc)

    @property
    def is_none(self) -> bool:
        return not (self.stages or self.series or self.scalars)

    @property
    def series_names(self) -> frozenset[str]:
        return frozenset(s.name for s in self.series)

    def to_manifest(self) -> dict[str, Any]:
        """The ``[operator."<n>".reports]`` table this declaration renders to."""
        out: dict[str, Any] = {}
        if self.stages:
            out["stages"] = list(self.stages)
        if self.series:
            out["series"] = [{"name": s.name, "unit": s.unit} for s in self.series]
        if self.scalars:
            out["scalars"] = list(self.scalars)
        return out

    @classmethod
    def from_manifest(cls, tbl: Optional[dict[str, Any]]) -> "ReportSpec":
        if not tbl:
            return cls.NONE
        series = []
        for item in tbl.get("series", ()) or ():
            if isinstance(item, dict):
                series.append(Series(str(item.get("name", "")), str(item.get("unit", "") or "")))
            else:
                series.append(Series(str(item), ""))
        return cls(stages=list(tbl.get("stages", ()) or ()), series=series,
                   scalars=list(tbl.get("scalars", ()) or ()))


ReportSpec.NONE = ReportSpec()


__all__ = [
    "CHECKPOINT_EVENT_TYPE", "DIVERGENCE_EVENT_TYPE", "METRICS_EVENT_TYPE",
    "PARAMETERS_EVENT_TYPE", "PROGRESS_EVENT_TYPE", "REPORT_EVENT_TYPES",
    "SCALARS_EVENT_TYPE", "STRICT_ENV", "ReportSpec", "Series",
    "UndeclaredReportError", "strict_reports_enabled",
]
