"""Schemas for monitor/propose — the human-gated proposal a crossed watch files."""
from __future__ import annotations

from pydantic import BaseModel, Field


class ProposeInput(BaseModel):
    metric: str = Field(..., description="The signal that crossed (e.g. 'chatbot-intents PSI').")
    value: float = Field(..., description="The observed value.")
    threshold: float = Field(..., description="The threshold it crossed.")
    proposed_workflow: str = Field(
        ..., description="The workflow a human is being asked to approve running (e.g. 'training/finetune').",
    )
    est_cost_usd: float = Field(0.0, description="Rough estimated cost of the proposed run (for the approval card).")


class ProposeOutput(BaseModel):
    metric: str
    value: float
    threshold: float
    proposed_workflow: str
    est_cost_usd: float
    approved: bool = Field(..., description="True once a human approved the proposal at the gate.")
