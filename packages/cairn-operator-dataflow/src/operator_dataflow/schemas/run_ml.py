"""Schemas for ml/run (ADR-0082 D7, #1074)."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from cairn.core.dataflow import Budget, DataflowSpec, StageRef

from operator_dataflow.schemas.run import DataflowRunOutput


class MlRunInput(BaseModel):
    """The ml/run binding surface — mirror of DataflowRunInput with the
    kind-neutral ``flow_ref`` key (the compiler's kind-matched door reads it)."""

    flow_ref: Optional[str] = Field(
        None, description="Pack-addressed kind: ml flow, e.g. 'training/sft-train'.",
    )
    stages: list[dict[str, Any]] = Field(
        default_factory=list, description="Inline stage list: [{stage, config?, resources?}].",
    )
    input_artifacts: list[str] = Field(
        default_factory=list,
        description="Dataset REFERENCES the flow consumes (immutable URI+hash, ADR-0080).",
    )
    config: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(
        default_factory=dict, description="{max_cost_usd?, max_rows?, max_seconds?}.",
    )
    parent_run: str = Field("", description="Run lineage (#989); usually set by --from.")

    def to_spec(self, stages: Optional[list[StageRef]] = None) -> DataflowSpec:
        # DataflowSpec is the engine-neutral FLOW spec (both kinds travel in
        # it); the ref stays control-side — engines only ever see stages, so
        # the caller passes the RESOLVED list when the input carried a ref.
        return DataflowSpec(
            stages=stages if stages is not None
            else [StageRef.model_validate(s) for s in self.stages],
            input_artifacts=list(self.input_artifacts),
            config=dict(self.config),
            budget=Budget.model_validate(self.budget),
            parent_run=self.parent_run,
        )


class MlRunOutput(DataflowRunOutput):
    """Same scalar contract as the data door: stats + registered artifact ids;
    rows/tensors never appear (the candidate travels as a URI)."""
