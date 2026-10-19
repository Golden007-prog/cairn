"""Pydantic schemas for data/profile. The heavy models (DataExpectation, the
verdict) live in cairn.services.data and are reused here verbatim."""
from __future__ import annotations

from pydantic import BaseModel, Field

from cairn.services.data import DataExpectation, RoleReport


class DataProfileInput(BaseModel):
    source: str = Field("", description="Path / file:// URI / HF id to load (when rows not given inline).")
    rows: list[dict] = Field(default_factory=list, description="Inline sample rows (e.g. a Playground upload).")
    expectation: DataExpectation = Field(..., description="What the workflow expects — semantic roles, not a wire format.")
    sample_limit: int = Field(1000, ge=1, description="Max records to load/profile.")


class DataProfileOutput(BaseModel):
    ok: bool = Field(..., description="False when the verdict is BLOCK.")
    verdict: str = Field(..., description="pass | degrade | block")
    record_count: int = 0
    detected_format: str = "unknown"
    missing_required: list[str] = Field(default_factory=list)
    degraded: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    note: str = ""
    roles: list[RoleReport] = Field(default_factory=list)
