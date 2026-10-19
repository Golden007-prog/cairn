"""`append_event` must not silently drop events under concurrent writers (#1509).

The old shape computed the sequence with `SELECT MAX(seq)` and then inserted in
a separate statement. Under READ COMMITTED that is not serialized — and there is
no row to lock for a seq that does not exist yet — so the API server and the
Temporal worker could pick the same seq. The second INSERT violated the
`(run_id, seq)` primary key, and **both** call sites
(`dispatch/dispatcher.py`, `runtime/temporal/activities.py`) catch and continue.
The event was lost, not retried.

Two levels of coverage, because the interesting failure needs two processes:

* **Hermetic** — drive the retry loop with a stub connection. Proves a lost race
  is retried rather than swallowed, and that sustained contention raises instead
  of returning a run-event with a bogus seq.
* **Live** — real concurrent writers against Postgres, skipped unless
  ``PGSTORE_DSN`` is set (the repo's existing convention).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import uuid

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.storage.postgres.runs import (
    _EVENT_SEQ_MAX_ATTEMPTS,
    _LOCK_RUN_EVENTS_SQL,
    PostgresRunStore,
)


class _StubConn:
    """An asyncpg-ish connection whose `fetchval` returns a scripted series.

    ``None`` models "another writer took this seq" — the ON CONFLICT DO NOTHING
    branch, which returns no row.
    """

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0
        self.executed: list[str] = []

    async def fetchval(self, *_a, **_kw):
        self.calls += 1
        return self._results.pop(0) if self._results else None

    async def execute(self, sql, *_a):
        self.executed.append(sql)

    @contextlib.asynccontextmanager
    async def transaction(self):
        yield


class _StubPool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _store_with(conn):
    store = PostgresRunStore(dsn="postgres://stub/stub")
    pool = _StubPool(conn)

    async def _get_pool():
        return pool

    store._get_pool = _get_pool  # type: ignore[method-assign]
    return store


def test_first_attempt_wins_is_a_single_round_trip():
    conn = _StubConn([1])
    ev = asyncio.run(_store_with(conn).append_event("run-1", "queued", {"a": 1}))
    assert ev.seq == 1
    assert conn.calls == 1, "an uncontended append must not pay for retries"
    assert conn.executed == [_LOCK_RUN_EVENTS_SQL], "the per-run lock comes first (#2544)"
    assert ev.data == {"a": 1}


def test_a_lost_race_is_retried_not_swallowed():
    """THE regression. Two Nones = two lost races; the event must still land."""
    conn = _StubConn([None, None, 7])
    ev = asyncio.run(_store_with(conn).append_event("run-1", "node", {}))
    assert ev.seq == 7
    assert conn.calls == 3


def test_sustained_contention_raises_rather_than_returning_a_bogus_seq():
    """Callers treat append_event as best-effort and log. Returning seq=0 would
    corrupt timeline ORDERING for every later consumer, which is worse than
    losing one event — so give up loudly."""
    conn = _StubConn([None] * (_EVENT_SEQ_MAX_ATTEMPTS + 5))
    with pytest.raises(RuntimeError, match="contention"):
        asyncio.run(_store_with(conn).append_event("run-1", "node", {}))
    assert conn.calls == _EVENT_SEQ_MAX_ATTEMPTS, "must stop at the attempt cap"


def test_the_retry_budget_is_bounded():
    """A retry loop with no ceiling is a hang, not a fix."""
    assert 1 < _EVENT_SEQ_MAX_ATTEMPTS <= 32


# ─── live concurrency (gated on PGSTORE_DSN, per repo convention) ──────


@pytest.mark.asyncio
async def test_concurrent_writers_get_unique_consecutive_seqs():
    dsn = os.environ.get("PGSTORE_DSN")
    if not dsn:
        pytest.skip("PGSTORE_DSN not set — skipping live Postgres integration")
    pytest.importorskip("asyncpg")

    store = PostgresRunStore(dsn=dsn)
    # A fresh id per run: `run_events` persists across test runs, so a
    # pid-derived id accumulates rows and the second run asserts against 80
    # events. (Caught by running this file twice.)
    run_id = f"seq-race-{uuid.uuid4()}"
    try:
        # No explicit init — the schema is ensured lazily inside _get_pool.
        n = 40
        # Every writer races every other writer for the same run's sequence.
        await asyncio.gather(*(
            store.append_event(run_id, "node", {"i": i}) for i in range(n)
        ))
        events = await store.get_events(run_id)
        seqs = sorted(e.seq for e in events)
        assert len(events) == n, f"{n - len(events)} events were DROPPED"
        assert seqs == list(range(1, n + 1)), "sequence must be dense and unique"
        assert len({e.data["i"] for e in events}) == n, "no payload may be lost"
    finally:
        await store.close()
