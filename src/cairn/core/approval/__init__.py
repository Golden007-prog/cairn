"""Approval — the non-bypassable HITL gate decision (ADR-0034).

Core owns WHETHER an operation needs human approval; the durable realization
(LangGraph interrupt / Temporal signal) lives in the runtime. The decision is
deployment-owned (profile + policy keyed on the operator's pack.toml
risk/capabilities), never pack-owned — see :mod:`cairn.core.approval.resolver`.
"""
from cairn.core.approval.resolver import (
    ApprovalGate,
    resolve_gate,
)

__all__ = ["ApprovalGate", "resolve_gate"]
