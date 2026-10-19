"""``operator_dataflow.engine.runner`` — a re-export kept for one release (#2583).

The kernel runner is core: :mod:`cairn.core.dataflow.runner` (ADR-0129 D1).
This module keeps the pre-#2583 import path working for bridges and scripts
that still spell it; new code imports from core. Deprecated: it is removed
after the next release (ADR-0129 D4). No warning is emitted on purpose — a
worker that runs thousands of legs must not log once per import.
"""
from __future__ import annotations

from cairn.core.dataflow.runner import BudgetExceeded, run_dataflow

__all__ = ["BudgetExceeded", "run_dataflow"]
