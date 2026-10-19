"""PostgresWorkflowConfigStore first-use init (#1833, run 3 of the fresh-machine gate).

On a fresh Postgres the console's first paint fires several requests at once.
The store used to publish ``self._pool`` BEFORE its ``CREATE TABLE`` statements
ran, so the second concurrent caller got a pool and queried a table that did not
exist yet: two 500s (``relation "input_presets" does not exist``) on the very
first page, and never again — the kind of bug a warm dev database cannot show.
This drives two concurrent first calls through a fake pool whose schema
creation is deliberately slow and asserts nobody sees the pool early.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from cairn.core import workflow_config_store as wcs


class _Conn:
    def __init__(self, log: list[str], gate: asyncio.Event) -> None:
        self._log, self._gate = log, gate

    async def execute(self, sql: str, *_: object) -> str:
        # the first schema statement parks until the test releases it, so the
        # second caller has every chance to sneak past a naive implementation
        if not self._log:
            await self._gate.wait()
        self._log.append(sql.split("(")[0].strip())
        return "CREATE TABLE"


class _Pool:
    def __init__(self, log: list[str], gate: asyncio.Event) -> None:
        self._log, self._gate = log, gate

    @asynccontextmanager
    async def acquire(self):
        yield _Conn(self._log, self._gate)


@pytest.mark.asyncio
async def test_concurrent_first_calls_wait_for_the_schema(monkeypatch):
    log: list[str] = []
    gate = asyncio.Event()
    pool = _Pool(log, gate)

    async def fake_shared_pool(_dsn: str):
        return pool

    monkeypatch.setattr("cairn.core.storage.postgres.pool.shared_pool", fake_shared_pool)
    # the optional dep gate: the import must succeed for the store to proceed
    import sys, types
    monkeypatch.setitem(sys.modules, "asyncpg", types.ModuleType("asyncpg"))

    store = wcs.PostgresWorkflowConfigStore("postgres://x")
    seen_before_schema: list[int] = []

    async def caller() -> None:
        p = await store._get_pool()
        assert p is pool
        # by the time ANY caller holds the pool, all three tables exist
        seen_before_schema.append(len(log))

    t1 = asyncio.create_task(caller())
    t2 = asyncio.create_task(caller())
    await asyncio.sleep(0)          # both callers are now inside _get_pool
    await asyncio.sleep(0)
    assert seen_before_schema == [], "a caller got the pool before the schema ran"
    gate.set()
    await asyncio.gather(t1, t2)
    assert seen_before_schema == [5, 5], seen_before_schema
    # the DDL runs inside the cross-replica schema lock (#2544)
    assert log == ["SELECT pg_advisory_lock",
                   "CREATE TABLE IF NOT EXISTS workflow_configs",
                   "CREATE TABLE IF NOT EXISTS workflow_presets",
                   "CREATE TABLE IF NOT EXISTS input_presets",
                   "SELECT pg_advisory_unlock"], "schema ran exactly once"
