"""Schemas for monitor/threshold — the generic signal/threshold detector (D4)."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ThresholdInput(BaseModel):
    metric: str = Field(
        ..., description="Human name of the signal being watched (e.g. 'chatbot-intents PSI').",
    )
    threshold: float = Field(..., description="The value the signal is compared against.")
    comparator: str = Field(
        "gt",
        description="How to compare value vs threshold: gt (>), gte (>=), lt (<), lte (<=). "
                    "Crossed = comparison is True (e.g. gt: value > threshold → drift).",
    )
    # ── the signal source (exactly one; checked in priority order) ──
    value: Optional[float] = Field(
        None, description="Inline metric value (highest priority — for tests / a caller that already has it).",
    )
    source_path: Optional[str] = Field(
        None, description="Path to a JSON file to read the metric from (with `source_key`).",
    )
    source_url: Optional[str] = Field(
        None, description="HTTP(S) GET returning JSON to read the metric from (with `source_key`).",
    )
    source_key: str = Field(
        "value",
        description="Dotted key into the JSON at source_path/source_url (e.g. 'metrics.psi'). Default 'value'.",
    )


class ThresholdOutput(BaseModel):
    metric: str = Field(..., description="The watched signal's name.")
    value: float = Field(..., description="The metric value that was read.")
    threshold: float = Field(..., description="The threshold it was compared against.")
    comparator: str = Field(..., description="The comparator applied.")
    crossed: bool = Field(..., description="True when the threshold was crossed (the trigger condition).")
    source: str = Field(..., description="Where the value came from: inline | file | http.")
