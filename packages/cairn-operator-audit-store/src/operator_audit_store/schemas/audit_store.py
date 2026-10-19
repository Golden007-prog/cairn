"""Pydantic schemas for audit/store (the node path)."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class AuditWriteInput(BaseModel):
    action: str = Field(..., description="Dotted verb, e.g. 'tool.call'.")
    outcome: str = Field("ok", description="ok | deny | error.")
    actor: Optional[str] = Field(None, description="Who triggered it (client/skill).")
    target: Optional[str] = Field(None, description="What it acted on (model, server/tool).")
    run_id: Optional[str] = None
    ts: str = Field("", description="ISO timestamp; filled by the sink if blank.")
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuditWriteOutput(BaseModel):
    ok: bool = True
    hash: str = Field("", description="sha256 chain hash of the appended row.")
