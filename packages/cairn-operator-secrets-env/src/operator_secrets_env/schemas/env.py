"""Schemas for secrets/env (the operation-node path).

The node path is a PRESENCE check only — it never returns a secret value (that
would leak it into state / the run trace). Operators read actual values via the
scoped ``ctx.secrets`` injected by the runtime, never as node output.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class SecretCheckInput(BaseModel):
    name: str = Field(..., description="Environment variable name to check for presence.")


class SecretCheckOutput(BaseModel):
    present: bool = Field(..., description="Whether the env var is set (value never returned).")
