"""Run / pipeline status-transition guard (ADR-0086 H3, #1245).

ONE legality table, enforced at the STORAGE choke point — every status write
goes through a store's ``update()``/``put()``, so guarding there covers all ~25
call sites (dispatcher, pipeline bridge, leg executor, reapers, Temporal
activities) without a sweep, and is race-safe: the check runs against the row
the store just read, inside its own lock/transaction.

Why this exists: two shipped incidents in one week were illegal-transition
bugs — #1241 (a paused pipeline's gate approval recursed into an advance that
silently dropped every downstream leg) and the #977/#1239 stale ``running``
columns on dead runs. Prefect encodes the same scar tissue as ~31 orchestration
rules (``PreventPendingTransitions``, ``HandleFlowTerminalStateTransitions``,
``PreventDuplicateTransitions`` …); this module is the cairn-sized version —
the table IS the policy, the test file is the checklist.

Semantics (deliberately conservative):

* ``to == current`` — a no-op re-write is always legal (records are re-``put``
  for non-status mutations constantly; terminal re-writes of the SAME status
  are idempotent completions racing, not corruption).
* Terminal states are IMMUTABLE — ``done``/``failed``/``cancelled`` (and
  ``expired`` for runs) never transition again. A late crash-belt trying to
  fail an already-cancelled run is exactly the write this refuses.
* An illegal transition refuses the WHOLE write (not just the status field) and
  logs a warning with both states and the caller's hint — a partial write with
  the status silently dropped would leave e.g. ``ended_at`` stamped on a run
  whose status never became terminal.

The guard REFUSES rather than raises: the racing writer is usually a
belt-and-braces path (crash belt, reaper, reconciler) whose loss already
happened elsewhere; raising would turn a benign race into a new failure mode.
Callers that must know consult the return value.
"""
from __future__ import annotations

import logging
from typing import Mapping, Optional

logger = logging.getLogger("cairn.core.storage.transitions")

#: Terminal statuses per entity — immutable once reached.
RUN_TERMINAL = frozenset({"done", "failed", "cancelled", "expired"})
PIPELINE_TERMINAL = frozenset({"done", "failed", "cancelled"})

#: Allowed transitions (current → set of nexts). Self-transitions are always
#: allowed and not listed. Anything absent is illegal.
RUN_TRANSITIONS: Mapping[str, frozenset[str]] = {
    # queued → done covers degenerate fast paths (a stubbed/coalesced run may
    # terminalize without an observed running write).
    "queued": frozenset({"running", "awaiting_approval", "done", "failed", "cancelled"}),
    "running": frozenset({"awaiting_approval", "done", "failed", "cancelled"}),
    # expired only from awaiting_approval (#896 — the approval reaper's TTL).
    "awaiting_approval": frozenset({"running", "done", "failed", "cancelled", "expired"}),
    "done": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "expired": frozenset(),
}

PIPELINE_TRANSITIONS: Mapping[str, frozenset[str]] = {
    # Forward JUMPS are legal (queued → done/paused): the Temporal read-model
    # mirror (`pipeline_mirror_state`) writes whatever state the workflow is
    # authoritatively in, and a short pipeline may never surface intermediate
    # states to the store. The protections that matter are no-backward and
    # terminal-immutability — not observing every hop.
    "queued": frozenset({"running", "paused", "done", "failed", "cancelled"}),
    "running": frozenset({"paused", "done", "failed", "cancelled"}),
    "paused": frozenset({"running", "done", "failed", "cancelled"}),
    "done": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


def check_transition(entity: str, current: Optional[str], to: str) -> bool:
    """Pure legality check. ``entity`` is ``"run"`` or ``"pipeline"``.

    ``current is None`` (no prior record — a create) and self-transitions are
    always legal. An UNKNOWN current status is treated as legal-from (never
    brick a record written by a newer/older version); an unknown TARGET is
    illegal (nothing may transition into a status this version can't name).
    """
    if current is None or current == to:
        return True
    table = PIPELINE_TRANSITIONS if entity == "pipeline" else RUN_TRANSITIONS
    allowed = table.get(current)
    if allowed is None:  # unknown current — permissive (forward/backward compat)
        return True
    return to in allowed


def guard_run_update(current_status: str, fields: dict, *, run_id: str = "") -> bool:
    """Gatekeeper for the runs stores' ``update()``: True = apply the update.

    False = the update contains an ILLEGAL status transition and the store must
    apply NOTHING (see module docstring for why whole-write refusal). Updates
    that don't touch ``status`` always pass.
    """
    to = fields.get("status")
    if to is None or check_transition("run", current_status, str(to)):
        return True
    logger.warning(
        "refused illegal run transition %s → %s (run=%s, fields=%s) — "
        "terminal states are immutable; see ADR-0086 H3",
        current_status, to, run_id, sorted(fields),
    )
    return False


def guard_pipeline_put(previous_status: Optional[str], new_status: str,
                       *, pipeline_id: str = "") -> bool:
    """Gatekeeper for the pipeline stores' ``put()``: True = apply the put.

    ``previous_status`` is the stored record's status (None when this put is
    the create). False = the put would move the pipeline through an illegal
    transition and must be dropped whole.
    """
    if check_transition("pipeline", previous_status, new_status):
        return True
    logger.warning(
        "refused illegal pipeline transition %s → %s (pipeline=%s) — "
        "terminal states are immutable; see ADR-0086 H3",
        previous_status, new_status, pipeline_id,
    )
    return False


__all__ = [
    "PIPELINE_TERMINAL", "PIPELINE_TRANSITIONS", "RUN_TERMINAL",
    "RUN_TRANSITIONS", "check_transition", "guard_pipeline_put",
    "guard_run_update",
]
