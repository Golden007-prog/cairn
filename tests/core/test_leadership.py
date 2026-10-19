"""#1533 — one replica owns the fleet's singleton periodic loops.

The failure this prevents is concrete: N replicas each running the cron
scheduler means one `schedule:` step fires N runs per tick — N× the spend and
N× the audit trail, from the product whose pitch is that governed work happens
once, on purpose. The reaper / retention / reconciler / RAG sweeps are the
same shape.

What must hold:

  1. **No Postgres ⇒ always leader.** A single-node or SQLite deployment must
     not silently stop reaping and reconciling; it is its own leader.
  2. **Managed loops start on promotion and STOP on demotion.** A demoted
     replica that keeps its loops running is precisely the duplication this
     module exists to prevent.
  3. **An unreachable database is a LOST election, not a crash** — and the
     process keeps retrying, so a partition heals into a promotion.
  4. (live PG) **Exactly one of N contenders wins**, and when the winner
     stands down a follower is promoted — the failover story, with no lease
     and no clock.
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.leadership import Leadership


class _Loop:
    """Stand-in for a reaper/reconciler: records start/stop like the real ones
    (sync `start()` / `shutdown()`)."""

    def __init__(self) -> None:
        self.running = False
        self.starts = 0
        self.stops = 0

    def start(self) -> None:
        self.running = True
        self.starts += 1

    def shutdown(self) -> None:
        self.running = False
        self.stops += 1


# ── no Postgres: single node is its own leader ───────────────────────────


async def test_without_postgres_the_process_is_always_leader(monkeypatch):
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    loop_ = _Loop()
    lead = Leadership(role="t", poll_seconds=0.05)
    lead.manage("reaper", start=loop_.start, stop=loop_.shutdown)
    try:
        await lead.start()
        assert lead.is_leader and loop_.running, (
            "a single-node deployment must keep reaping and reconciling")
        assert loop_.starts == 1
        await asyncio.sleep(0.2)   # the retry loop must not restart it
        assert loop_.starts == 1, "re-election must not double-start a loop"
    finally:
        await lead.stop()
    assert loop_.stops == 1 and not loop_.running


async def test_sqlite_style_dsn_is_not_mistaken_for_postgres(monkeypatch):
    monkeypatch.setenv("OBS_STORAGE", "sqlite:///tmp/x.db")
    lead = Leadership(role="t", poll_seconds=0.05)
    try:
        await lead.start()
        assert lead.is_leader
    finally:
        await lead.stop()


# ── promotion / demotion move the managed loops ──────────────────────────


async def test_demotion_stops_the_loops(monkeypatch):
    """The core guarantee: losing the election stops the work, or the whole
    module is decoration."""
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    a, b = _Loop(), _Loop()
    lead = Leadership(role="t", poll_seconds=10)
    lead.manage("one", start=a.start, stop=a.shutdown)
    lead.manage("two", start=b.start, stop=b.shutdown)
    await lead.start()
    assert a.running and b.running

    await lead._demote()
    assert not a.running and not b.running
    assert (a.stops, b.stops) == (1, 1)
    await lead.stop()


async def test_a_loop_without_stop_is_reported_loudly(monkeypatch, caplog):
    """A managed loop with no stop() keeps running after demotion — that is the
    duplication bug. It is allowed (some loops have no handle) but must warn."""
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    started = []
    lead = Leadership(role="t", poll_seconds=10)
    lead.manage("no-stop", start=lambda: started.append(1))
    await lead.start()
    with caplog.at_level("WARNING"):
        await lead._demote()
    assert any("has no stop()" in r.message for r in caplog.records)
    await lead.stop()


async def test_start_failure_of_one_loop_does_not_block_the_others(monkeypatch):
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    good = _Loop()
    lead = Leadership(role="t", poll_seconds=10)

    def boom() -> None:
        raise RuntimeError("nope")

    lead.manage("bad", start=boom)
    lead.manage("good", start=good.start, stop=good.shutdown)
    try:
        await lead.start()
        assert good.running, "one broken loop must not cost the rest"
    finally:
        await lead.stop()


async def test_async_start_and_stop_are_awaited(monkeypatch):
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    seen = []

    async def astart() -> None:
        seen.append("start")

    async def astop() -> None:
        seen.append("stop")

    lead = Leadership(role="t", poll_seconds=10)
    lead.manage("async-loop", start=astart, stop=astop)
    await lead.start()
    await lead.stop()
    assert seen == ["start", "stop"]


# ── an unreachable database is a lost election, not a crash ──────────────


async def test_unreachable_postgres_makes_this_process_a_follower(monkeypatch, caplog):
    monkeypatch.setenv("OBS_STORAGE", "postgresql://nobody:x@127.0.0.1:1/void")
    loop_ = _Loop()
    lead = Leadership(role="t", poll_seconds=10)
    lead.manage("reaper", start=loop_.start, stop=loop_.shutdown)
    try:
        with caplog.at_level("WARNING"):
            await lead.start()   # must not raise
        assert not lead.is_leader and not loop_.running, (
            "if we cannot reach the DB we cannot know we are leader — another "
            "replica may be, so this one must not also run the loops")
        assert any("FOLLOWER" in r.message for r in caplog.records)
    finally:
        await lead.stop()


async def test_an_exhausted_pool_is_a_lost_election_not_a_hang(monkeypatch):
    """#2543: each contender gets its OWN pool, an acquire that cannot get a
    connection times out into a lost election, and stop() closes the pool."""
    import cairn.core.leadership as leadership_mod
    import cairn.core.storage.postgres.pool as pool_mod

    pools: dict[str, _HungPool] = {}

    async def _fake_shared_pool(dsn, *, purpose="", min_size=1, max_size=None):
        return pools.setdefault(purpose, _HungPool())

    monkeypatch.setenv("OBS_STORAGE", "postgresql://u@127.0.0.1:5432/db")
    monkeypatch.setattr(pool_mod, "shared_pool", _fake_shared_pool)
    monkeypatch.setattr(leadership_mod, "ACQUIRE_TIMEOUT_SECONDS", 0.05)
    leads = [Leadership(role="t", poll_seconds=10) for _ in range(2)]
    try:
        for lead in leads:
            await asyncio.wait_for(lead.start(), timeout=5)  # bounded, never hangs
        assert not any(x.is_leader for x in leads)
        assert len(pools) == 2, "two contenders for one role must not share a pool"
    finally:
        for lead in leads:
            await lead.stop()
    assert all(p.closed for p in pools.values()), "stop() must close the pool"


class _HungPool:
    """A pool whose only connection is pinned elsewhere: acquire never returns
    unless bounded by its timeout."""

    def __init__(self) -> None:
        self.closed = False

    async def acquire(self, *, timeout=None):
        await asyncio.wait_for(asyncio.Event().wait(), timeout)

    async def close(self) -> None:
        self.closed = True


# ── live Postgres: exactly one winner, and real failover ─────────────────


def _dsn() -> str:
    dsn = os.environ.get("PGSTORE_DSN")
    if not dsn:
        pytest.skip("PGSTORE_DSN not set — skipping live Postgres integration")
    pytest.importorskip("asyncpg")
    return dsn


async def test_live_pg_exactly_one_of_three_wins_and_failover_promotes(monkeypatch):
    """Three contenders = three replicas. One wins; when it stands down, a
    follower is promoted on its next poll — no lease, no clock, just the lock
    moving because the holder's session let go."""
    monkeypatch.setenv("OBS_STORAGE", _dsn())
    import uuid

    role = f"t1533-{uuid.uuid4().hex[:8]}"
    loops = [_Loop(), _Loop(), _Loop()]
    leads = [Leadership(role=role, poll_seconds=0.5) for _ in loops]
    for lead, lp in zip(leads, loops):
        lead.manage("reaper", start=lp.start, stop=lp.shutdown)
    try:
        for lead in leads:
            await lead.start()
        winners = [i for i, x in enumerate(leads) if x.is_leader]
        assert len(winners) == 1, f"exactly one leader, got {winners}"
        assert sum(lp.running for lp in loops) == 1

        # the leader stands down; a follower must take over
        await leads[winners[0]].stop()
        for _ in range(20):
            await asyncio.sleep(0.5)
            if any(x.is_leader for i, x in enumerate(leads) if i != winners[0]):
                break
        promoted = [i for i, x in enumerate(leads) if i != winners[0] and x.is_leader]
        assert len(promoted) == 1, "a follower must be promoted after the leader leaves"
        assert loops[promoted[0]].running
    finally:
        for lead in leads:
            await lead.stop()
