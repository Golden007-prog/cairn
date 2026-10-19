"""Pipeline-tier resume: the run store IS the checkpoint (#1130, ADR-0084 D6).

**Why no Temporal at this tier.** The durable state is already persisted:
completed legs are terminal runs with persisted outputs, an in-flight leg is a
run holding an engine handle, and a paused gate is an ``awaiting_approval`` row.
Resume is therefore **plan re-evaluation** — read the record, decide what each
step needs — rather than replay of an execution history.

This generalizes ADR-0083 D7 ("durability derived per segment") one tier up:

===============  ==========================================================
pipeline         run store + plan (this module)
agentic leg      LangGraph checkpointer / ``interrupt()`` (unchanged)
data/ml leg      engine-owned resume; the control plane holds only the handle
===============  ==========================================================

The plan is **derived, never stored**: stored resume state could drift from the
record it describes, and then a restart would act on a stale picture.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Steps that still need submitting after a restart.
_RESUBMIT = ("pending", "queued")
#: Steps waiting on a human or an external signal — restart must neither
#: auto-advance them nor re-ask.
_AWAITING = ("awaiting_approval", "awaiting_advance", "awaiting_schedule",
             "awaiting_event")
#: Steps that are finished, one way or another. Never re-run.
_TERMINAL = ("done", "skipped", "failed", "cancelled")


@dataclass
class ResumePlan:
    """What a restarted control plane should do with each step of one pipeline."""

    resubmit: list[str] = field(default_factory=list)
    reattach: list[str] = field(default_factory=list)
    awaiting: list[str] = field(default_factory=list)
    orphaned: list[str] = field(default_factory=list)
    iterations: dict[str, int] = field(default_factory=dict)

    @property
    def is_noop(self) -> bool:
        return not (self.resubmit or self.reattach or self.orphaned)


def plan_resume(rec: Any) -> ResumePlan:
    """Decide, from the persisted record alone, how to continue this pipeline.

    Every step status maps to exactly one action — an unclassified step on
    restart is a pipeline that silently stalls, so the buckets are exhaustive
    and a parametrized test pins each one.
    """
    plan = ResumePlan()
    if str(getattr(rec, "status", "")) in ("done", "failed", "cancelled"):
        return plan

    for step_id, st in (getattr(rec, "step_state", None) or {}).items():
        status = str(getattr(st, "status", "") or "pending")
        iteration = int(getattr(st, "iteration", 0) or 0)
        if iteration:
            # A loop's position is persisted, so a restart neither restarts the
            # loop from zero (wasted spend) nor skips ahead (unverified
            # convergence) — #1129's unrolling picks up here.
            plan.iterations[step_id] = iteration

        if status in _TERMINAL:
            continue
        if status in _AWAITING:
            plan.awaiting.append(step_id)
        elif status == "running":
            # The engine kept working through the restart. Re-submitting would
            # duplicate PAID compute, so a handle is required to continue —
            # without one there is nothing to re-attach to, and guessing could
            # double-charge. That becomes a decision, not a silent retry.
            if getattr(st, "handle", None):
                plan.reattach.append(step_id)
            else:
                plan.orphaned.append(step_id)
        elif status in _RESUBMIT:
            plan.resubmit.append(step_id)
        else:  # unknown status — treat as needing attention, never as done
            plan.orphaned.append(step_id)

    return plan


def reattachable_legs(rec: Any) -> list[dict]:
    """Engine handles for legs still in flight — the re-attach tokens."""
    return [
        h for st in (getattr(rec, "step_state", None) or {}).values()
        if str(getattr(st, "status", "")) == "running"
        and (h := getattr(st, "handle", None))
    ]


__all__ = ["ResumePlan", "plan_resume", "reattachable_legs"]
