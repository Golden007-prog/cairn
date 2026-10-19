"""Pydantic schemas for redact/text (the node path)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class RedactTextInput(BaseModel):
    text: str = Field(..., description="Text to scan and redact.")


class RedactTextOutput(BaseModel):
    ok: bool = True
    text: str = Field(..., description="Redacted text (placeholders substituted).")
    redacted: bool = Field(..., description="True if any span was redacted.")
    span_count: int = 0
    kinds: list[str] = Field(default_factory=list, description="Distinct recognizer kinds hit.")
