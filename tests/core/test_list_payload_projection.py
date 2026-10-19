"""List views must not hydrate `result_payloads` (#1508).

`result_payloads` is persisted with no size cap, and every list endpoint strips
it at serialization anyway — so `/runs?limit=100` (the console's poll) and the
insights endpoints at `limit=500` were parsing potentially multi-MB blobs to
render list rows. The cost grew with accumulated history, not with load.

The projection is `data - 'result_payloads'` in SQL, so the payloads never
leave the database.

The dangerous part of this change is the SILENT direction: `result_payloads`
defaults to `[]`, so a caller that reads it off a projected row sees "no
payloads" rather than an error. The platform's `routers/playground.py` is exactly such a
caller — the what-if history extracts a metric from payloads — so it passes
`include_payloads=True`; the platform pins that it still must
(packages/cairn-platform/tests/test_suggestions_api.py).
"""
from __future__ import annotations

import asyncio
import os
import uuid

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.storage.postgres.runs import PostgresRunStore


def _dsn():
    dsn = os.environ.get("PGSTORE_DSN")
    if not dsn:
        pytest.skip("PGSTORE_DSN not set — skipping live Postgres integration")
    pytest.importorskip("asyncpg")
    return dsn


def _run(store, run_id, payload_kb=0):
    from cairn.core.engine.schema import TriggerEvent
    from cairn.core.storage import Run

    r = Run(
        id=run_id,
        trigger=TriggerEvent(source="pattern", matched_pattern="x", service="svc"),
        lane="realtime", fingerprint=f"fp-{run_id}", status="done",
        workspace_id="default",
    )
    if payload_kb:
        r.result_payloads = [{"node": "big", "output": {"blob": "x" * (payload_kb * 1024)}}]
    return r


# ─── the projection ────────────────────────────────────────────────────


def test_list_omits_payloads_by_default_and_returns_them_on_request():
    dsn = _dsn()

    async def _go():
        store = PostgresRunStore(dsn=dsn)
        ws = f"proj-{uuid.uuid4().hex[:8]}"
        try:
            r = _run(store, f"run-{ws}", payload_kb=64)
            r.workspace_id = ws
            await store.create(r)

            lean = await store.list(workspace_id=ws, limit=10)
            assert len(lean) == 1
            assert lean[0].result_payloads == [], "payloads must not be hydrated for list rows"
            # everything else must survive the projection
            assert lean[0].id == r.id
            assert lean[0].status == "done"
            assert lean[0].fingerprint == r.fingerprint

            full = await store.list(workspace_id=ws, limit=10, include_payloads=True)
            assert full[0].result_payloads, "include_payloads=True must return them"
            assert len(full[0].result_payloads[0]["output"]["blob"]) == 64 * 1024
        finally:
            await store.close()

    asyncio.run(_go())


def test_get_by_id_still_returns_payloads():
    """Only LIST views are projected. `GET /runs/{id}` is the detail view and
    must keep the payloads, or the report a user clicks into would be empty."""
    dsn = _dsn()

    async def _go():
        store = PostgresRunStore(dsn=dsn)
        rid = f"run-detail-{uuid.uuid4().hex[:8]}"
        try:
            r = _run(store, rid, payload_kb=8)
            await store.create(r)
            got = await store.get(rid)
            assert got is not None
            assert got.result_payloads, "the detail view must keep payloads"
        finally:
            await store.close()

    asyncio.run(_go())


def test_the_payloads_never_leave_the_database():
    """The point of a SQL projection rather than a Python filter: measure the
    bytes actually returned by the query."""
    dsn = _dsn()

    async def _go():
        store = PostgresRunStore(dsn=dsn)
        ws = f"bytes-{uuid.uuid4().hex[:8]}"
        try:
            for i in range(5):
                r = _run(store, f"run-{ws}-{i}", payload_kb=128)
                r.workspace_id = ws
                await store.create(r)

            pool = await store._get_pool()
            async with pool.acquire() as conn:
                lean = await conn.fetchval(
                    "SELECT sum(length((data - 'result_payloads')::text)) "
                    "FROM runs WHERE workspace_id = $1", ws)
                full = await conn.fetchval(
                    "SELECT sum(length(data::text)) FROM runs WHERE workspace_id = $1", ws)
            # 5 runs x 128KB of payload
            assert full - lean > 5 * 128 * 1024, "the payloads should dominate the blob"
            assert lean < full / 10, f"projection must be a large win: {lean} vs {full}"
        finally:
            await store.close()

    asyncio.run(_go())


# ─── the silent-empty hazard ───────────────────────────────────────────


def test_the_dispatcher_default_is_projected():
    """The whole point: callers get the cheap query unless they ask otherwise."""
    import inspect

    from cairn.core.dispatch.dispatcher import RunDispatcher

    sig = inspect.signature(RunDispatcher.list)
    assert sig.parameters["include_payloads"].default is False


def test_every_store_tier_accepts_the_flag():
    """Protocol parity — a tier that ignores it is fine, a tier that rejects it
    breaks the dispatcher on that backend."""
    import inspect

    from cairn.core.storage.memory.runs import InMemoryRunStore
    from cairn.core.storage.sqlite.runs import SqliteRunStore

    for tier in (InMemoryRunStore, SqliteRunStore, PostgresRunStore):
        assert "include_payloads" in inspect.signature(tier.list).parameters, tier.__name__
