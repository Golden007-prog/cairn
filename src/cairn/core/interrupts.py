"""Typed interrupt kinds (ADR-0065, #551).

cairn pauses a run via LangGraph ``interrupt()`` for two very different reasons:
a **human-approval gate** (ADR-0034) or a **remote job** the run is waiting on
(ADR-0038). The payload passed to ``interrupt()`` carries a ``kind`` so the
runtimes can SURFACE them differently — an approval pause is
``awaiting_approval`` (a human must act); a remote-job pause is just ``running``
(the work is executing remotely and the control loop drives it). Without this a
running remote job masquerades as a pending approval in the console.

Kept tiny and dependency-free so the dispatcher, the Temporal workflow, and the
operator runtime can all import it without a cycle.
"""
from __future__ import annotations

from typing import Any

KIND_APPROVAL = "approval"
KIND_REMOTE_JOB = "remote_job"


def interrupt_kind(value: Any) -> str:
    """Classify an ``interrupt()`` payload (the value LangGraph surfaces as
    ``__interrupt__[i].value``).

    Prefers an explicit ``kind`` field; falls back to the legacy shape
    (``{"remote_job": True, ...}``) so in-flight payloads without a ``kind`` still
    resolve. Anything else — including a bare ``ApprovalRequest`` — is an approval.
    """
    data: Any = value
    if data is not None and not isinstance(data, dict) and hasattr(data, "model_dump"):
        try:
            data = data.model_dump(mode="json")
        except Exception:  # noqa: BLE001 — a non-serializable payload is just "approval"
            data = None
    if isinstance(data, dict):
        kind = data.get("kind")
        if kind in (KIND_APPROVAL, KIND_REMOTE_JOB):
            return kind
        if data.get("remote_job"):
            return KIND_REMOTE_JOB
    return KIND_APPROVAL


def is_remote_job_interrupt(value: Any) -> bool:
    """True if the pause is a remote-job wait (not a human-approval gate)."""
    return interrupt_kind(value) == KIND_REMOTE_JOB


__all__ = ["KIND_APPROVAL", "KIND_REMOTE_JOB", "interrupt_kind", "is_remote_job_interrupt"]
