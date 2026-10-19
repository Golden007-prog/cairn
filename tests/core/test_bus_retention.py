"""`bus_messages` retention, and the deliberate refusal to prune `run_events`
(#1510).

The dispatcher writes every event twice — `run_events` (durable record) and
`bus_messages` (transport buffer for live delivery and SSE catch-up). Neither
was ever pruned.

These tests pin BOTH halves of the decision, because the second half is the one
a future reader is most likely to "fix":

  * `bus_messages` IS pruned — duplicated data, indexed `created_at`.
  * `run_events` is NOT — it is the run timeline a governance product exists to
    keep, and it has no indexed timestamp to prune by, so a scheduled delete
    would be a full table scan with a text comparison on the one table that
    only grows.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.storage import retention


def _dsn():
    dsn = os.environ.get("PGSTORE_DSN")
    if not dsn:
        pytest.skip("PGSTORE_DSN not set — skipping live Postgres integration")
    pytest.importorskip("asyncpg")
    return dsn


async def _seed(conn, topic, *, count, age_days):
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS bus_messages (
            message_id     TEXT PRIMARY KEY,
            topic          TEXT NOT NULL,
            correlation_id TEXT,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            data           JSONB NOT NULL
        )""")
    for i in range(count):
        await conn.execute(
            "INSERT INTO bus_messages (message_id, topic, correlation_id, created_at, data) "
            "VALUES ($1, $2, $3, now() - ($4 || ' days')::interval, $5::jsonb)",
            f"{topic}-{i}", topic, topic, str(age_days), json.dumps({"i": i}),
        )


# ─── the sweep ─────────────────────────────────────────────────────────


def test_old_messages_go_and_recent_ones_stay():
    dsn = _dsn()

    async def _go():
        import asyncpg
        topic = f"t-{uuid.uuid4().hex[:8]}"
        conn = await asyncpg.connect(dsn)
        try:
            await _seed(conn, f"{topic}-old", count=20, age_days=30)
            await _seed(conn, f"{topic}-new", count=5, age_days=1)

            deleted = await retention.sweep_once(dsn, days=7)
            assert deleted >= 20

            old = await conn.fetchval(
                "SELECT count(*) FROM bus_messages WHERE topic = $1", f"{topic}-old")
            new = await conn.fetchval(
                "SELECT count(*) FROM bus_messages WHERE topic = $1", f"{topic}-new")
            assert old == 0, "messages past the window must be gone"
            assert new == 5, "messages inside the window must survive"
        finally:
            await conn.execute("DELETE FROM bus_messages WHERE topic LIKE $1", f"{topic}%")
            await conn.close()

    asyncio.run(_go())


def test_a_zero_window_deletes_nothing():
    """`0` is the off switch — a deployment must be able to keep everything."""
    dsn = _dsn()

    async def _go():
        import asyncpg
        topic = f"t-{uuid.uuid4().hex[:8]}"
        conn = await asyncpg.connect(dsn)
        try:
            await _seed(conn, topic, count=10, age_days=400)
            assert await retention.sweep_once(dsn, days=0) == 0
            assert await conn.fetchval(
                "SELECT count(*) FROM bus_messages WHERE topic = $1", topic) == 10
        finally:
            await conn.execute("DELETE FROM bus_messages WHERE topic = $1", topic)
            await conn.close()

    asyncio.run(_go())


def test_the_sweep_is_idempotent():
    dsn = _dsn()

    async def _go():
        import asyncpg
        topic = f"t-{uuid.uuid4().hex[:8]}"
        conn = await asyncpg.connect(dsn)
        try:
            await _seed(conn, topic, count=10, age_days=30)
            first = await retention.sweep_once(dsn, days=7)
            second = await retention.sweep_once(dsn, days=7)
            assert first >= 10
            assert second == 0, "a second pass must find nothing left to do"
        finally:
            await conn.execute("DELETE FROM bus_messages WHERE topic = $1", topic)
            await conn.close()

    asyncio.run(_go())


# ─── the decisions, pinned ─────────────────────────────────────────────


def test_run_events_is_never_touched():
    """THE guard. `run_events` is the run timeline — evidence, not cache — and
    it has no indexed timestamp to prune by. If someone later adds it to this
    sweep, that must be a deliberate act with a migration, not a one-line
    extension of a hygiene task."""
    import inspect

    src = inspect.getsource(retention)
    # the module may NAME run_events in prose; it must never DELETE from it
    assert "DELETE FROM run_events" not in src
    assert "delete from run_events" not in src.lower()


def test_the_sweeper_is_off_without_postgres():
    """sqlite is a disposable dev file; pruning it risks a developer's local
    history for no benefit."""
    assert retention.RetentionSweeper(None).enabled is False
    assert retention.RetentionSweeper("").enabled is False


def test_the_window_is_env_owned(monkeypatch):
    monkeypatch.setenv("CAIRN_BUS_RETENTION_DAYS", "30")
    assert retention.bus_retention_days() == 30.0
    monkeypatch.setenv("CAIRN_BUS_RETENTION_DAYS", "0")
    assert retention.RetentionSweeper("postgres://x/y").enabled is False


def test_a_garbage_window_falls_back_to_the_default(monkeypatch):
    """A typo in an env var must not silently mean 'delete everything'."""
    monkeypatch.setenv("CAIRN_BUS_RETENTION_DAYS", "seven")
    assert retention.bus_retention_days() == retention.DEFAULT_BUS_RETENTION_DAYS


def test_batching_is_bounded():
    """An unbounded DELETE on a multi-million-row table holds a lock for as
    long as it takes; both caps must stay in place."""
    assert 0 < retention.DELETE_BATCH <= 50_000
    assert 0 < retention.MAX_BATCHES_PER_SWEEP <= 10_000
