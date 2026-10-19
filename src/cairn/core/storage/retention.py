"""#1510 — TTL retention for ``bus_messages``, the table that grows forever.

``RunDispatcher._emit`` writes every run event TWICE: once to ``run_events``
via ``store.append_event``, and once to ``bus_messages`` via ``bus.publish``.
Neither was ever pruned, so at 10x run volume ``bus_messages`` becomes the
largest object in the database and every scan cost climbs with total history.

**The two tables are not equivalent, and this module deliberately prunes only
one of them.**

``bus_messages``  A transport buffer for live delivery and SSE catch-up. The
                  same payload is durably in ``run_events``, so pruning old
                  rows loses nothing. It also has ``created_at TIMESTAMPTZ``
                  and an index on it, so the delete is index-served.
                  → pruned, 7-day default.

``run_events``    The run TIMELINE — what the console renders and what an
                  operator reads to answer "what happened". In a governance
                  product that is evidence, not cache.
                  → **NOT pruned here**, for two independent reasons:

                    1. Deleting it is a governance decision, not a hygiene one.
                    2. It has no ``created_at`` — only ``ts TEXT`` — and no
                       index on it, so any time-based delete would be a full
                       table scan with a text comparison, on the one table
                       that only grows. Doing that on a schedule would be
                       worse than the problem.

                  Pruning it needs an indexed timestamp column first; that
                  belongs with the #1471 migration work. Tracked on #1510.

Modelled on :mod:`cairn.core.approval.reaper` — same env-owned knob, same
"0 disables", same periodic-task shape.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Transport buffer, duplicated in run_events — a week is generous for
#: catch-up and replay. 0 (or negative) disables the sweep entirely.
DEFAULT_BUS_RETENTION_DAYS = 7.0
DEFAULT_SWEEP_HOURS = 6.0
#: Rows per DELETE. Bounded so each statement holds its lock briefly, which
#: matters on a table that may have millions of rows by the first sweep.
DELETE_BATCH = 5_000
#: Stop after this many batches in one pass; the next sweep continues. Prevents
#: a first-ever run against a huge table from holding the loop for hours.
MAX_BATCHES_PER_SWEEP = 200


def _float_env(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number — using %g", name, raw, default)
        return default


def bus_retention_days() -> float:
    """Days of ``bus_messages`` to keep. ``<= 0`` disables pruning."""
    return _float_env("CAIRN_BUS_RETENTION_DAYS", DEFAULT_BUS_RETENTION_DAYS)


def sweep_hours() -> float:
    return _float_env("CAIRN_RETENTION_SWEEP_HOURS", DEFAULT_SWEEP_HOURS)


_DELETE_BATCH_SQL = """
    WITH doomed AS (
        SELECT ctid FROM bus_messages
        WHERE created_at < now() - ($1 || ' days')::interval
        ORDER BY created_at
        LIMIT $2
    )
    DELETE FROM bus_messages t USING doomed d WHERE t.ctid = d.ctid
"""


async def sweep_once(dsn: str, *, days: Optional[float] = None) -> int:
    """Run one retention pass over ``bus_messages``. Returns rows deleted.

    Postgres-only: the sqlite dev tier is a single disposable file, and pruning
    it would trade a real risk (deleting a developer's local history) for no
    benefit.
    """
    keep_days = bus_retention_days() if days is None else days
    if keep_days <= 0:
        return 0

    import asyncpg  # noqa: PLC0415 — optional dep (storage-postgres)

    deleted = 0
    conn = await asyncpg.connect(dsn)
    try:
        for _ in range(MAX_BATCHES_PER_SWEEP):
            status = await conn.execute(_DELETE_BATCH_SQL, str(keep_days), DELETE_BATCH)
            # asyncpg returns e.g. "DELETE 5000"
            n = int(status.rsplit(" ", 1)[-1]) if status else 0
            deleted += n
            if n < DELETE_BATCH:
                break
        else:
            logger.info(
                "retention sweep hit the %d-batch cap with rows still expiring; "
                "the next sweep continues", MAX_BATCHES_PER_SWEEP,
            )
    finally:
        await conn.close()

    if deleted:
        logger.info("retention: deleted %d bus_messages older than %gd", deleted, keep_days)
    return deleted


class RetentionSweeper:
    """Periodic ``bus_messages`` retention. Same shape as ``ApprovalReaper``."""

    def __init__(self, dsn: Optional[str] = None, *, sweep_seconds: Optional[float] = None):
        self.dsn = dsn
        self.sweep_seconds = (
            sweep_seconds if sweep_seconds is not None else sweep_hours() * 3600.0
        )
        self._task: Optional[asyncio.Task[Any]] = None

    @property
    def enabled(self) -> bool:
        """Off without a Postgres DSN, and off when the window is <= 0."""
        return bool(self.dsn) and bus_retention_days() > 0

    def start(self) -> None:
        """Start the loop. Idempotent; a no-op when retention is disabled or the
        deployment is not on Postgres."""
        if not self.enabled or (self._task and not self._task.done()):
            return
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "retention sweeper started (bus_messages=%gd, sweep=%gs; run_events kept forever)",
            bus_retention_days(), self.sweep_seconds,
        )

    def shutdown(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("retention sweeper stopped")

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.sweep_seconds)
                await sweep_once(self.dsn or "")
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 — a bad sweep must not kill the loop
                logger.warning("retention sweep failed: %s", exc)


__all__ = [
    "DEFAULT_BUS_RETENTION_DAYS",
    "DEFAULT_SWEEP_HOURS",
    "RetentionSweeper",
    "bus_retention_days",
    "sweep_once",
]
