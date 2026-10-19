"""``operator_dataflow.stages`` — a re-export kept for one release (#2583).

The stage registry is core: :mod:`cairn.core.dataflow.stage_loader` (ADR-0129
D1). The pack's ``cairn.dataflow_stages`` entry points (``noop``, ``noop.slow``)
still resolve through this module, so they are unchanged; new code imports the
loader from core. Deprecated: removed after the next release (ADR-0129 D4).
"""
from __future__ import annotations

from cairn.core.dataflow.stage_loader import (
    StageFn,
    load_stages,
    noop,
    noop_slow,
    stage_load_failures,
    stage_versions,
)

__all__ = ["StageFn", "load_stages", "noop", "noop_slow", "stage_load_failures", "stage_versions"]
