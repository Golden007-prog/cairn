"""The conformance suite validated against its reference StubEngine (#1046).

This proves the SUITE itself is runnable and complete — the builtin engine
(#1048) and every adapter (Metaflow/SkyPilot, #1054) subclass the same
``DataflowEngineConformance`` and must pass these identical cases."""
from __future__ import annotations

from cairn.core.dataflow import DataflowSpec, StageRef
from cairn.core.dataflow.conformance import DataflowEngineConformance, StubEngine


class TestStubEngineConformance(DataflowEngineConformance):
    supports_cancel_midrun = True
    supports_budget_abort = False  # stub does not self-enforce budgets

    def make_engine(self) -> StubEngine:
        return StubEngine()

    def slow_spec(self) -> DataflowSpec:
        return DataflowSpec(stages=[StageRef(stage=StubEngine.HOLD_STAGE)])
