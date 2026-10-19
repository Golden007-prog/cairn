"""Schemas for artifact/promote (ADR-0081 P2b, #1052)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class ArtifactPromoteInput(BaseModel):
    """Move a governed tag onto a registered artifact."""

    tag: str = Field(..., description="Governed tag to move, e.g. 'dataset@prod'.")
    artifact_id: str = Field(..., description="Registry id (content hash) to point the tag at.")
    reason: str = Field("", description="Why — recorded on the audit row the gate writes.")


class ArtifactPromoteOutput(BaseModel):
    tag: str = ""
    artifact_id: str = ""
    previous_artifact_id: str = Field(
        "", description="What the tag pointed at before (empty on first promotion) — the rollback target.",
    )
    uri: str = Field("", description="Where the now-promoted artifact lives.")
    promoted: bool = False
