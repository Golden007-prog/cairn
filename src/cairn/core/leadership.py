"""Single-writer election for the control plane's periodic loops (#1533).

Some things must run **once per fleet**, not once per replica. The cron
scheduler is the sharp case: with N replicas each running it, one `schedule:`
step fires N runs per tick — N× the spend, N× the audit trail, and a
governance product quietly doing the same governed thing three times. The
reaper, retention sweep, run reconciler and RAG freshness scheduler are all
the same shape: idempotent enough not to corrupt anything, wasteful and
confusing to run N times.

This module elects one replica to own them. Same substrate as everything else
in ADR-0091 Amendment 1 — **Postgres, never Temporal** — so electing a leader
never drags in a workflow engine a Ray/Spark/Dask deployment has no use for.

**Mechanism.** ``pg_try_advisory_lock`` on one well-known key, held on a
dedicated connection for the life of the process. Holding it *is* leadership;
there is no lease to renew and no clock to trust. If the leader crashes, is
OOM-killed, or is simply network-partitioned from Postgres, its session ends
and the lock is released by the database — the next follower's retry wins it.
That is the whole failover story, and it has no timestamp arithmetic in it.

**Followers retry, they do not idle forever.** A follower re-tries on
``poll_seconds`` (default 15s), so a leader's death costs at most one poll
interval of paused periodic work. The trade is deliberate: a shorter interval
means faster failover and more idle queries; the loops being elected here run
on minute-plus schedules, so 15s is already far inside the noise.

**Not everything periodic wants to be a singleton.** A durable trigger inbox
is a work queue — multiple consumers is the *point*, and electing one would
throw away the throughput a durable inbox exists to provide. Only loops whose
work is "scan the world and act on it" belong here.

**Degrade rule matches the rest of #1532/#1533**: when there is no Postgres
(single-process dev, SQLite deployments), every process is trivially the
leader. A control plane that cannot reach Postgres must not silently stop
reaping and reconciling — it is a single node, and a single node is always
its own leader.
"""

from __future__ import annotations
from cairn.core.settings import getenv

import asyncio
import logging
import os
import uuid
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("cairn.leadership")

#: Advisory-lock key namespace. Distinct from the #1532 section namespaces so a
#: hashtext collision between a pipeline id and the leader key is impossible to
#: confuse when debugging.
_LEADER_KEY = "cairn:leader:{role}"

_TRY_LOCK_SQL = "SELECT pg_try_advisory_lock(hashtext($1))"

#: How often a follower re-tries. See the module docstring on the trade. Public:
#: ``core/liveness.py`` paces its owner-lock re-acquire loop by it (ADR-0125 D2).
POLL_SECONDS = 15.0

#: Bound on taking the election connection (#2543). An exhausted or wedged pool
#: is a LOST election — the follower retries next poll — never a hung startup.
#: Public: the owner lock's connection is bounded by it too.
ACQUIRE_TIMEOUT_SECONDS = 5.0

StartFn = Callable[[], Any]
StopFn = Callable[[], Any]


def postgres_dsn() -> str:
    """Same selector as the run store, #1530's budget backend and #1532's
    sections (#427: one env var, one meaning). Public: ``core/liveness.py``
    picks its lock backend by it."""
    url = getenv("CAIRN_STORAGE", "").strip()
    return url if url.startswith(("postgres://", "postgresql://")) else ""


#: The earlier private spellings, kept as aliases.
_POLL_SECONDS = POLL_SECONDS
_ACQUIRE_TIMEOUT_SECONDS = ACQUIRE_TIMEOUT_SECONDS
_postgres_dsn = postgres_dsn


async def _maybe_await(value: Any) -> None:
    if isinstance(value, Awaitable):
        await value


class Leadership:
    """Elects this process to own the fleet's singleton periodic loops.

    Components register with :meth:`manage` and are started when leadership is
    won and stopped when it is lost, so a follower that is promoted after the
    leader dies picks the loops up without a restart::

        leader = Leadership(role="periodic")
        leader.manage("reconciler", start=reconciler.start, stop=reconciler.stop)
        await leader.start()
    """

    def __init__(self, *, role: str = "periodic",
                 poll_seconds: float = POLL_SECONDS) -> None:
        self.role = role
        self.poll_seconds = poll_seconds
        self._is_leader = False
        self._conn: Any = None
        self._pool: Any = None
        # Per-instance pool key (#2543): two contenders in one process must not
        # share the size-1 pool, or the holder's pinned connection starves the
        # other's acquire. A uuid, not id(self): a reused id would hand a new
        # instance an earlier instance's closed pool from the shared cache.
        self._pool_purpose = f"leader:{role}:{uuid.uuid4().hex[:12]}"
        self._task: Optional[asyncio.Task] = None
        self._managed: list[tuple[str, StartFn, Optional[StopFn]]] = []
        self._started: set[str] = set()

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    def manage(self, name: str, *, start: StartFn, stop: Optional[StopFn] = None) -> None:
        """Register a loop this process runs ONLY while it holds leadership.

        ``stop`` is optional but strongly preferred: without it a demoted
        leader keeps running the loop, which is the exact duplication this
        module exists to prevent."""
        self._managed.append((name, start, stop))

    # ── election ─────────────────────────────────────────────────────

    async def _acquire(self) -> bool:
        """Try to take the lock. ``True`` when this process is (still) leader."""
        dsn = postgres_dsn()
        if not dsn:
            return True  # no shared store ⇒ single node ⇒ always its own leader
        try:
            if self._conn is None:
                if self._pool is None:
                    from cairn.core.storage.postgres.pool import (  # noqa: PLC0415 — optional dep (storage-postgres)
                        shared_pool,
                    )

                    # Its own pool: this connection is pinned for the PROCESS
                    # lifetime, so it must never come from the store pool (or
                    # #1532's short-lived section pools). Size 1 — one leader,
                    # one connection — and one pool per INSTANCE (#2543).
                    self._pool = await shared_pool(
                        dsn, purpose=self._pool_purpose, min_size=0, max_size=1)
                self._conn = await self._pool.acquire(timeout=ACQUIRE_TIMEOUT_SECONDS)
            got = await self._conn.fetchval(_TRY_LOCK_SQL, _LEADER_KEY.format(role=self.role))
            return bool(got)
        except Exception:  # noqa: BLE001 — an unreachable DB is a LOST election, not a crash
            logger.warning(
                "leadership[%s]: election query failed — treating this process "
                "as a FOLLOWER until Postgres is reachable again. Singleton "
                "loops are paused here; another replica should hold them.",
                self.role, exc_info=True,
            )
            await self._drop_conn()
            return False

    async def _drop_conn(self) -> None:
        """Release the election connection. Dropping it releases the advisory
        lock (session-scoped), which is what lets a successor take over."""
        conn, self._conn = self._conn, None
        if conn is None or self._pool is None:
            return
        try:
            await self._pool.release(conn)
        except Exception:  # noqa: BLE001 — a dead connection is already released
            logger.debug("leadership[%s]: connection release failed", self.role,
                         exc_info=True)

    # ── managed components ───────────────────────────────────────────

    async def _promote(self) -> None:
        self._is_leader = True
        logger.info("leadership[%s]: ELECTED — starting %d singleton loop(s): %s",
                    self.role, len(self._managed),
                    ", ".join(n for n, _s, _t in self._managed) or "(none)")
        for name, start, _stop in self._managed:
            if name in self._started:
                continue
            try:
                await _maybe_await(start())
                self._started.add(name)
            except Exception:  # noqa: BLE001 — one bad loop must not cost the others
                logger.exception("leadership[%s]: %s failed to start", self.role, name)

    async def _demote(self) -> None:
        self._is_leader = False
        logger.warning("leadership[%s]: LOST leadership — stopping singleton loops",
                       self.role)
        for name, _start, stop in reversed(self._managed):
            if name not in self._started:
                continue
            self._started.discard(name)
            if stop is None:
                logger.warning(
                    "leadership[%s]: %s has no stop() — it keeps running on a "
                    "demoted replica, which is the duplication this exists to "
                    "prevent. Give it a stop().", self.role, name)
                continue
            try:
                await _maybe_await(stop())
            except Exception:  # noqa: BLE001 — best effort on the way down
                logger.exception("leadership[%s]: %s failed to stop", self.role, name)

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Contest the election now, then keep contesting in the background."""
        if await self._acquire():
            await self._promote()
        else:
            logger.info("leadership[%s]: another replica holds it — standing by "
                        "as follower (retry every %.0fs)", self.role, self.poll_seconds)
        self._task = asyncio.create_task(self._loop(), name=f"leadership:{self.role}")

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.poll_seconds)
                held = await self._acquire()
                if held and not self._is_leader:
                    await self._promote()
                elif not held and self._is_leader:
                    # We had it and lost it — the DB says someone else does now,
                    # or our session died. Stop the loops before anything else.
                    await self._demote()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the election loop must never die
                logger.exception("leadership[%s]: election loop error", self.role)

    async def stop(self) -> None:
        """Stand down: cancel the loop, stop managed loops, release the lock."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutdown
                pass
        if self._is_leader:
            await self._demote()
        await self._drop_conn()
        await self._close_pool()

    async def _close_pool(self) -> None:
        """Close this instance's pool so a stopped contender holds no
        connection. Its cache entry in ``pool.py`` stays (there is no per-key
        evict there), so the purpose is renewed: a restarted instance gets a
        fresh pool, never its own closed one, and ``reset_pools()`` re-closing
        the old entry is a no-op."""
        pool, self._pool = self._pool, None
        self._pool_purpose = f"leader:{self.role}:{uuid.uuid4().hex[:12]}"
        if pool is None:
            return
        try:
            await asyncio.wait_for(pool.close(), timeout=ACQUIRE_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 — shutdown; never block standing down
            logger.debug("leadership[%s]: pool close failed — terminating",
                         self.role, exc_info=True)
            pool.terminate()


__all__ = ["ACQUIRE_TIMEOUT_SECONDS", "Leadership", "POLL_SECONDS", "postgres_dsn"]
