"""Regression tests for #977 — the ``status`` COLUMN is authoritative on read.

A Run is serialized whole into the ``data`` blob, but its lifecycle status is
ALSO mirrored into the indexed ``status`` column (the one ``list(status=…)`` /
``find_active_by_fingerprint`` filter on). When a terminal transition updates
the column WITHOUT round-tripping the serialized Run (the #976 zombie path —
a sweep/cancel that flips the column directly), the blob's status goes stale:
column says ``done``/``cancelled``, blob still says ``running``. Before the
fix, ``get()``/``list()`` hydrated entirely from the blob → runs showed
"running" in the console forever (live prod: 16-day-old zombies with Cancel
buttons).

Acceptance (issue #977): a row whose blob says ``running`` but whose column
says ``done`` must list AND get as ``done`` — for both the sqlite and
postgres stores. The postgres store is covered at three levels: a pure
``_hydrate`` unit test, SQL-shape assertions (every read SELECTs the status
column alongside ``data``), and a live roundtrip gated on ``PGSTORE_DSN``
(same skip pattern as test_postgres_run_store.py).
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.engine.schema import TriggerEvent
from cairn.core.storage import Run, SqliteRunStore
from cairn.core.storage.postgres.runs import (
    PostgresRunStore,
    _SELECT_RUN_DATA_SQL,
    _SELECT_RUN_DATA_WS_SQL,
    _hydrate as _pg_hydrate,
)
from cairn.core.storage.sqlite.runs import _hydrate as _sqlite_hydrate


def _run(id="run-977-1", fp="fp-977", status="running"):
    return Run(
        id=id,
        trigger=TriggerEvent(source="pattern", matched_pattern="x", service="svc"),
        lane="realtime", fingerprint=fp, status=status,
    )


def _stale_column_flip(store: SqliteRunStore, run_id: str, column_status: str):
    """Simulate the #976 zombie path: flip the indexed status column directly,
    leaving the serialized blob untouched (still says its old status)."""
    store._conn.execute(
        "UPDATE runs SET status=? WHERE id=?", (column_status, run_id)
    )
    store._conn.commit()


# ─── SQLite store ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sqlite_get_returns_column_status_over_stale_blob(tmp_path):
    store = SqliteRunStore(str(tmp_path / "runs.db"))
    await store.create(_run(status="running"))
    _stale_column_flip(store, "run-977-1", "done")
    # sanity: the blob really is stale
    blob = store._conn.execute(
        "SELECT json_extract(data,'$.status') FROM runs WHERE id='run-977-1'"
    ).fetchone()[0]
    assert blob == "running"

    got = await store.get("run-977-1")
    assert got is not None
    assert got.status == "done"          # column wins
    store.close()


@pytest.mark.asyncio
async def test_sqlite_list_returns_column_status_over_stale_blob(tmp_path):
    store = SqliteRunStore(str(tmp_path / "runs.db"))
    await store.create(_run(status="running"))
    _stale_column_flip(store, "run-977-1", "done")

    listed = await store.list()
    assert [r.status for r in listed] == ["done"]
    # filter consistency: the same run must not APPEAR running in an
    # unfiltered list while being EXCLUDED from a status=running filter
    assert await store.list(status="running") == []
    assert [r.id for r in await store.list(status="done")] == ["run-977-1"]
    store.close()


@pytest.mark.asyncio
async def test_sqlite_stale_blob_run_is_not_active(tmp_path):
    """find_active_by_fingerprint filters on the column — a zombie whose blob
    says running must not resurface as an active dedup hit, and if a row DOES
    match on an active column value the hydrated status must be the column's."""
    store = SqliteRunStore(str(tmp_path / "runs.db"))
    await store.create(_run(status="running"))
    _stale_column_flip(store, "run-977-1", "cancelled")
    assert await store.find_active_by_fingerprint("fp-977") is None

    # inverse divergence: column active, blob stale-terminal → column wins too
    await store.create(_run(id="run-977-2", fp="fp-977b", status="running"))
    store._conn.execute(
        "UPDATE runs SET data=json_set(data,'$.status','failed') WHERE id='run-977-2'"
    )
    store._conn.commit()
    active = await store.find_active_by_fingerprint("fp-977b")
    assert active is not None and active.status == "running"
    store.close()


def test_sqlite_hydrate_prefers_column_and_tolerates_data_only_rows():
    run = _run(status="running")
    assert _sqlite_hydrate((run.model_dump_json(), "done")).status == "done"
    # callers that SELECT only data get the blob as-is
    assert _sqlite_hydrate((run.model_dump_json(),)).status == "running"


# ─── Postgres store (unit + SQL shape; live roundtrip gated on DSN) ───


def test_pg_hydrate_prefers_column_over_stale_blob():
    run = _run(status="running")
    row = {"data": run.model_dump_json(), "status": "done"}
    assert _pg_hydrate(row).status == "done"
    # agreeing / absent column → blob untouched
    assert _pg_hydrate({"data": run.model_dump_json(), "status": "running"}).status == "running"
    assert _pg_hydrate({"data": run.model_dump_json(), "status": None}).status == "running"


def test_pg_read_sql_selects_status_column():
    """Every read path must SELECT the status column alongside data, or
    _hydrate has nothing authoritative to apply."""
    assert "status" in _SELECT_RUN_DATA_SQL
    assert "status" in _SELECT_RUN_DATA_WS_SQL


@pytest.fixture
def _pg_dsn():
    dsn = os.environ.get("PGSTORE_DSN")
    if not dsn:
        pytest.skip("PGSTORE_DSN not set — skipping live Postgres integration")
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        pytest.skip("storage-postgres optional dep group not installed")
    return dsn


@pytest.mark.asyncio
async def test_pg_get_and_list_return_column_status_over_stale_blob(_pg_dsn):
    """Live acceptance test: blob says running, column says done → get AND
    list report done. Run with PGSTORE_DSN='postgresql://…' set."""
    store = PostgresRunStore(dsn=_pg_dsn)
    run_id = "run-977-pg-stale"
    pool = await store._get_pool()
    try:
        await store.create(_run(id=run_id, fp="fp-977-pg", status="running"))
        # zombie path: flip the column only; blob stays 'running'
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE runs SET status='done' WHERE id=$1", run_id
            )
            blob = await conn.fetchval(
                "SELECT data->>'status' FROM runs WHERE id=$1", run_id
            )
        assert blob == "running"      # sanity: divergence is real

        got = await store.get(run_id)
        assert got is not None and got.status == "done"
        listed = {r.id: r.status for r in await store.list(limit=1000)}
        assert listed.get(run_id) == "done"
        assert await store.find_active_by_fingerprint("fp-977-pg") is None
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM runs WHERE id=$1", run_id)
        await store.close()
