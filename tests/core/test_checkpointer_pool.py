"""#632 — the async Postgres checkpointer pool must be bounded and self-evicting.

Root cause of the connection exhaustion behind #614: `_pg_pool_for_loop` built a
`AsyncConnectionPool(max_size=10)` per event loop, keyed by `id(loop)`, never
evicted — so transient loops leaked whole pools (10 × N-loops × N-procs overran
Postgres max_connections). These pin the pure logic: env-configurable bounded
size/idle, and eviction of pools whose loop has finished. (psycopg isn't in the
test venv, so the pool itself isn't built here — only the bookkeeping.)
"""
from __future__ import annotations

import weakref

import cairn.core.runtime.checkpointer as cp


class _FakeLoop:
    """Weakref-able stand-in for an asyncio loop with a controllable is_closed()."""

    def __init__(self, closed: bool = False):
        self._closed = closed

    def is_closed(self) -> bool:
        return self._closed


def test_pool_max_size_env(monkeypatch):
    monkeypatch.delenv("OBS_CHECKPOINTER_POOL_MAX", raising=False)
    assert cp._pg_pool_max_size() == 3            # default lowered from 10
    monkeypatch.setenv("OBS_CHECKPOINTER_POOL_MAX", "8")
    assert cp._pg_pool_max_size() == 8
    monkeypatch.setenv("OBS_CHECKPOINTER_POOL_MAX", "garbage")
    assert cp._pg_pool_max_size() == 3            # bad value → default
    monkeypatch.setenv("OBS_CHECKPOINTER_POOL_MAX", "0")
    assert cp._pg_pool_max_size() == 1            # floored at 1


def test_pool_max_idle_env(monkeypatch):
    monkeypatch.delenv("OBS_CHECKPOINTER_POOL_MAX_IDLE", raising=False)
    assert cp._pg_pool_max_idle() == 60.0         # default lowered from 600
    monkeypatch.setenv("OBS_CHECKPOINTER_POOL_MAX_IDLE", "30")
    assert cp._pg_pool_max_idle() == 30.0
    monkeypatch.setenv("OBS_CHECKPOINTER_POOL_MAX_IDLE", "nope")
    assert cp._pg_pool_max_idle() == 60.0


def test_prune_evicts_closed_and_gc_loops(monkeypatch):
    cp._PG_POOL_BY_LOOP.clear()
    cp._PG_SETUP_LOOPS.clear()

    live = _FakeLoop(closed=False)
    closed = _FakeLoop(closed=True)
    cp._PG_POOL_BY_LOOP[id(live)] = ("poolL", weakref.ref(live))
    cp._PG_POOL_BY_LOOP[id(closed)] = ("poolC", weakref.ref(closed))
    cp._PG_SETUP_LOOPS.update({id(live), id(closed)})

    # a GC'd loop: build a ref that already resolves to None
    gone = _FakeLoop()
    dead_ref = weakref.ref(gone)
    del gone
    cp._PG_POOL_BY_LOOP[123456] = ("poolG", dead_ref)
    assert dead_ref() is None  # (CPython refcounting) the loop is gone

    cp._prune_dead_pg_pools()

    assert id(live) in cp._PG_POOL_BY_LOOP          # a live loop's pool is kept
    assert id(closed) not in cp._PG_POOL_BY_LOOP    # a closed loop's pool is dropped
    assert 123456 not in cp._PG_POOL_BY_LOOP        # a GC'd loop's pool is dropped
    assert id(closed) not in cp._PG_SETUP_LOOPS     # setup marker pruned too
    assert id(live) in cp._PG_SETUP_LOOPS

    cp._PG_POOL_BY_LOOP.clear()
    cp._PG_SETUP_LOOPS.clear()
