"""Schemas for monitor/dispatch — launching what a human approved (#1162)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class DispatchInput(BaseModel):
    approved: bool = Field(..., description="The approval verdict carried from monitor/propose.")
    proposed_workflow: str = Field(
        ..., description="What to launch — `pipeline:<pack>/<name>` or a `<pack>/<template>` ref.",
    )
    metric: str = Field("", description="The signal that crossed — recorded as provenance on the launched run.")
    value: float = Field(0.0, description="The observed value at approval time.")
    threshold: float = Field(0.0, description="The threshold it crossed.")
    est_cost_usd: float = Field(0.0, description="The estimate the human approved. Stamped on the run so the authorisation and the spend can be compared.")
    inputs: dict = Field(default_factory=dict, description="Inputs for the launched run.")


class DispatchOutput(BaseModel):
    dispatched: bool = Field(..., description="False when the proposal was not approved — the no-spend path.")
    launched_ref: str = Field("", description="What was launched (empty when nothing was).")
    run_id: str = Field("", description="The governed run/pipeline this created.")
    reason: str = Field("", description="Why nothing was launched, when nothing was.")
