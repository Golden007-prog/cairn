"""Schemas for dataflow/run (ADR-0081 D1, #1048)."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from cairn.core.dataflow import Budget, DataflowSpec, StageRef


class DataflowRunInput(BaseModel):
    """The operator's binding surface — a thin veneer over DataflowSpec so
    operation YAMLs bind scalars/lists naturally."""

    dataflow_ref: Optional[str] = Field(
        None, description="Pack-addressed dataflow (first-class in P2a, #1051).",
    )
    stages: list[dict[str, Any]] = Field(
        default_factory=list, description="Inline stage list: [{stage, config?, resources?}].",
    )
    input_artifacts: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(
        default_factory=dict, description="{max_cost_usd?, max_rows?, max_seconds?}.",
    )
    parent_run: str = Field("", description="Run lineage (#989); usually set by --from.")

    def to_spec(self) -> DataflowSpec:
        return DataflowSpec(
            dataflow_ref=self.dataflow_ref,
            stages=[StageRef.model_validate(s) for s in self.stages],
            input_artifacts=list(self.input_artifacts),
            config=dict(self.config),
            budget=Budget.model_validate(self.budget),
            parent_run=self.parent_run,
        )


class DataflowRunOutput(BaseModel):
    """Scalar stats + registered artifact ids — what lands in findings. Rows
    never appear here (contract-enforced upstream)."""

    stats: dict[str, Any] = Field(default_factory=dict)
    artifact_ids: list[str] = Field(default_factory=list)
    artifact_uris: list[str] = Field(default_factory=list)
    engine: str = ""
    cost_usd: float = 0.0
    warnings: list[str] = Field(default_factory=list)
    summary: str = ""
