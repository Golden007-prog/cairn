"""Schedule catch-up + misfire policy — ADR-0086 H4 (#1246).

The Tier-1 scheduler is in-process apscheduler: a cron fire that falls inside
server downtime simply vanishes. For a governance plane that is an
**auditability** hole before it is a durability one — "why did the nightly
readiness run not happen last night?" had no answer in the system.

This module is the answer. Given a :class:`~cairn.core.storage.schedule_store.StoredSchedule`
and "now", it

1. enumerates the cron occurrences that fell between the schedule's watermark
   (its last successful fire, or the last window already accounted for) and
   now — using apscheduler's own :class:`CronTrigger` math, never hand-rolled
   cron parsing, and
2. applies the row's ``misfire`` policy:

   ``skip`` (default)
       nothing is fired; every missed window is **audited** (one
       ``schedule.missed`` event per window, or a single summarising event once
       the count exceeds :data:`MISSED_AUDIT_LIMIT`).
   ``fire_once``
       all missed windows coalesce into ONE immediate run, audited as a
       ``schedule.catchup`` carrying the coalesced count.
   ``fire_all``
       each missed window is replayed, hard-capped at :data:`FIRE_ALL_CAP`;
       the excess is audited as ``schedule.dropped``.

Default is ``skip`` so behaviour only changes where an operator opted in.

Audit goes through the existing seam (:func:`cairn.core.obs.audit.audit_emit`,
ADR-0028) — no new sink. Because that seam is a no-op until a deployment binds
``audit.sink``, every decision is ALSO written as a structured logger record so
the "why didn't it run" question is answerable from the engine log alone.

The Temporal runtime is untouched: it owns its own timers.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger("cairn.scheduler.catchup")

#: Do not fire; audit the missed windows.
SKIP = "skip"
#: Coalesce every missed window into one immediate run.
FIRE_ONCE = "fire_once"
#: Replay each missed window (bounded by :data:`FIRE_ALL_CAP`).
FIRE_ALL = "fire_all"

MISFIRE_POLICIES: tuple[str, ...] = (SKIP, FIRE_ONCE, FIRE_ALL)
DEFAULT_MISFIRE = SKIP

#: Sanity cap on ``fire_all`` — a week of downtime on a ``*/5`` cron must not
#: unleash 2000 runs. Excess is audited as dropped, not silently forgotten.
FIRE_ALL_CAP = 10
#: Above this many missed windows, ``skip`` emits ONE summarising audit event
#: instead of one per window (an audit log is evidence, not a firehose).
MISSED_AUDIT_LIMIT = 10
#: Hard bound on the occurrence walk. A minutely cron with a year-old watermark
#: is half a million occurrences; we stop, mark the plan truncated, and advance
#: the watermark to what we did account for. (The schedule's own next punctual
#: fire stamps ``last_fired_at`` anyway, so the backlog can't accumulate.)
SCAN_CAP = 1000


def normalize_policy(value: Any) -> str:
    """Coerce a stored ``misfire`` value to a known policy.

    Unknown/empty degrades to :data:`DEFAULT_MISFIRE` (loudly) — a typo in a
    stored row must not fire runs nobody asked for."""
    v = str(value or "").strip().lower()
    if not v:
        return DEFAULT_MISFIRE
    if v not in MISFIRE_POLICIES:
        logger.warning("unknown misfire policy %r — falling back to %r", value, DEFAULT_MISFIRE)
        return DEFAULT_MISFIRE
    return v


def parse_ts(value: Any) -> Optional[datetime]:
    """ISO-8601 string → aware UTC datetime; ``None`` when absent/unparseable.
    A naive timestamp is read as UTC (that is what the store writes)."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            logger.warning("unparseable schedule timestamp %r — ignoring", value)
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def missed_windows(
    cron: str,
    since: datetime,
    now: datetime,
    *,
    scan_cap: int = SCAN_CAP,
) -> tuple[list[datetime], bool]:
    """Cron occurrences in ``(since, now]``, plus a truncated flag.

    Uses apscheduler's :class:`CronTrigger` — ``get_next_fire_time(prev, prev)``
    yields the first occurrence strictly after ``prev``, so walking it from the
    watermark enumerates exactly the fires that downtime swallowed. Raises
    whatever ``from_crontab`` raises on a bad expression (callers log)."""
    trigger = CronTrigger.from_crontab(cron)
    out: list[datetime] = []
    prev = since
    while len(out) < scan_cap:
        nxt = trigger.get_next_fire_time(prev, prev)
        if nxt is None or nxt > now:
            return out, False
        out.append(nxt)
        prev = nxt
    return out, True


@dataclass(frozen=True)
class CatchupPlan:
    """What the catch-up pass decided for one schedule. Pure data — planning is
    separated from applying so the decision is unit-testable without a
    dispatcher."""

    schedule_id: str
    cron: str
    policy: str
    #: Every missed occurrence found (bounded by :data:`SCAN_CAP`).
    missed: list[datetime] = field(default_factory=list)
    #: True when the walk hit :data:`SCAN_CAP` — more windows exist before now.
    truncated: bool = False
    #: Occurrences that will actually be replayed (``skip`` → empty;
    #: ``fire_once`` → the most recent one; ``fire_all`` → up to the cap).
    fire_at: list[datetime] = field(default_factory=list)
    #: Missed windows discarded by the ``fire_all`` cap.
    dropped: int = 0

    def __bool__(self) -> bool:
        """Truthy only when there is something to report — the pass skips
        schedules that missed nothing."""
        return bool(self.missed)

    @property
    def missed_count(self) -> int:
        return len(self.missed)

    @property
    def fire_count(self) -> int:
        return len(self.fire_at)

    @property
    def watermark(self) -> Optional[datetime]:
        """The newest occurrence this plan accounts for. Stamped back on the row
        so a second restart does not re-audit (or re-fire) the same windows."""
        return self.missed[-1] if self.missed else None


def plan_catchup(row: Any, now: Optional[datetime] = None) -> CatchupPlan:
    """Decide what a schedule owes after downtime. Never fires anything.

    A row that has neither fired nor been accounted for (a freshly created
    schedule) has no history to replay: it returns an empty plan, so creating a
    schedule can never trigger a catch-up storm from cron epochs it never
    lived through."""
    now = now or datetime.now(timezone.utc)
    policy = normalize_policy(getattr(row, "misfire", ""))
    empty = CatchupPlan(schedule_id=row.id, cron=row.cron, policy=policy)

    fired = parse_ts(getattr(row, "last_fired_at", ""))
    seen = parse_ts(getattr(row, "last_missed_at", ""))
    marks = [m for m in (fired, seen) if m is not None]
    if not marks:
        return empty  # never fired, never audited → nothing to catch up
    since = max(marks)
    if since >= now:
        return empty

    missed, truncated = missed_windows(row.cron, since, now)
    if not missed:
        return empty

    if policy == FIRE_ONCE:
        fire_at, dropped = [missed[-1]], 0
    elif policy == FIRE_ALL:
        fire_at = missed[:FIRE_ALL_CAP]
        dropped = len(missed) - len(fire_at)
    else:  # SKIP
        fire_at, dropped = [], 0
    return CatchupPlan(
        schedule_id=row.id, cron=row.cron, policy=policy, missed=missed,
        truncated=truncated, fire_at=fire_at, dropped=dropped,
    )


async def _audit(action: str, row: Any, outcome: str = "ok", **metadata: Any) -> None:
    """One catch-up audit event on the existing seam (ADR-0028). Import is local
    so the scheduler module doesn't drag obs onto its import path."""
    from cairn.core.obs.audit import audit_emit  # noqa: PLC0415 — keep obs off the import path

    await audit_emit(
        action,
        outcome=outcome,
        actor="scheduler",
        target=f"schedule:{row.id}",
        schedule_id=row.id,
        schedule_name=getattr(row, "name", ""),
        workflow=getattr(row, "workflow", ""),
        cron=getattr(row, "cron", ""),
        **metadata,
    )


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


async def apply_catchup(
    row: Any,
    plan: CatchupPlan,
    fire: Callable[[str], Awaitable[Any]],
) -> int:
    """Audit ``plan`` and perform its fires. Returns the number of fires made.

    ``fire`` is the scheduler's own stored-schedule fire path, so a catch-up run
    is dispatched through exactly the same governed door as a punctual one.
    A fire that raises is logged and does not abort the remaining ones."""
    if not plan:
        return 0
    logger.warning(
        "schedule %s missed %d window(s) since last accounted fire "
        "(cron=%s policy=%s firing=%d dropped=%d truncated=%s)",
        plan.schedule_id, plan.missed_count, plan.cron, plan.policy,
        plan.fire_count, plan.dropped, plan.truncated,
    )

    if plan.policy == SKIP:
        if plan.missed_count <= MISSED_AUDIT_LIMIT:
            for when in plan.missed:
                await _audit("schedule.missed", row, policy=SKIP, window=_iso(when),
                             missed_count=plan.missed_count, fired=False)
        else:  # summarise — an audit trail, not a firehose
            await _audit("schedule.missed", row, policy=SKIP, fired=False,
                         missed_count=plan.missed_count, summarized=True,
                         truncated=plan.truncated,
                         first_window=_iso(plan.missed[0]), last_window=_iso(plan.missed[-1]))
        return 0

    fired = 0
    for i, when in enumerate(plan.fire_at, start=1):
        outcome = "ok"
        try:
            await fire(plan.schedule_id)
            fired += 1
        except Exception as exc:  # noqa: BLE001 — one bad catch-up must not stop the rest
            outcome = "error"
            logger.exception("schedule %s catch-up fire failed: %s", plan.schedule_id, exc)
        # audited AFTER the attempt so the trail records what happened, not what
        # was intended — a catch-up that failed to dispatch says so.
        await _audit(
            "schedule.catchup", row, outcome=outcome, policy=plan.policy, window=_iso(when),
            missed_count=plan.missed_count, coalesced=(plan.policy == FIRE_ONCE),
            replay_index=i, replay_of=plan.fire_count,
        )
    if plan.dropped:
        await _audit("schedule.dropped", row, policy=plan.policy, dropped=plan.dropped,
                     missed_count=plan.missed_count, cap=FIRE_ALL_CAP,
                     truncated=plan.truncated)
    return fired
