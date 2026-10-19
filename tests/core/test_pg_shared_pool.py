"""#614 — every Postgres store shares ONE asyncpg pool per DSN in a process.

Four store classes used to open their own pool (max_size 4–8 each); across them ×
server + worker + worker-remote that overran Postgres max_connections
(TooManyConnectionsError, 103/100 live). These pin the shared-pool contract with a
FAKE asyncpg (the venv doesn't ship the storage-postgres extra, which also keeps
the test hermetic — no live DB): count how many pools actually get created.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest


class _FakeConn:
    async def execute(self, *_a, **_k):
        return None


class _FakeAcquire:
    async def __aenter__(self):
        return _FakeConn()

    async def __aexit__(self, *_a):
        return False


class _FakePool:
    def __init__(self):
        self.closed = False

    def acquire(self):
        return _FakeAcquire()

    async def close(self):
        self.closed = True


@pytest.fixture
def fake_asyncpg(monkeypatch):
    """Inject a fake `asyncpg` whose create_pool records each call, so tests can
    assert HOW MANY pools were opened without a real Postgres."""
    calls: list[tuple] = []
    mod = types.ModuleType("asyncpg")

    async def _create_pool(dsn, **kw):
        calls.append((dsn, kw))
        return _FakePool()

    mod.create_pool = _create_pool  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "asyncpg", mod)
    return calls


@pytest.fixture(autouse=True)
def _reset(fake_asyncpg):
    yield
    # drop cached pools so the process-global cache doesn't leak across tests
    from cairn.core.storage.postgres.pool import reset_pools
    asyncio.new_event_loop().run_until_complete(reset_pools())


DSN = "postgres://u:p@h:5432/db"


# ── the helper ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_same_dsn_returns_one_pool(fake_asyncpg):
    from cairn.core.storage.postgres.pool import shared_pool
    a = await shared_pool(DSN)
    b = await shared_pool(DSN)
    assert a is b
    assert len(fake_asyncpg) == 1  # created exactly once


@pytest.mark.asyncio
async def test_distinct_dsns_get_distinct_pools(fake_asyncpg):
    from cairn.core.storage.postgres.pool import shared_pool
    a = await shared_pool(DSN)
    b = await shared_pool("postgres://u:p@h:5432/other")
    assert a is not b
    assert len(fake_asyncpg) == 2


@pytest.mark.asyncio
async def test_concurrent_first_callers_coalesce(fake_asyncpg):
    from cairn.core.storage.postgres.pool import shared_pool
    pools = await asyncio.gather(*[shared_pool(DSN) for _ in range(12)])
    assert all(p is pools[0] for p in pools)
    assert len(fake_asyncpg) == 1  # one creation despite 12 concurrent callers


@pytest.mark.asyncio
async def test_max_size_from_env(fake_asyncpg, monkeypatch):
    monkeypatch.setenv("OBS_PG_POOL_MAX", "25")
    from cairn.core.storage.postgres.pool import shared_pool
    await shared_pool(DSN)
    assert fake_asyncpg[0][1]["max_size"] == 25


# ── the actual #614 fix: N store classes → 1 pool ──────────────────────

@pytest.mark.asyncio
async def test_all_stores_share_one_pool(fake_asyncpg):
    """The regression that matters: the run store, eval ledger, RAG catalog and
    BYO-corpus store on the same DSN must open ONE pool between them, not four."""
    from cairn.core.storage.eval_ledger import PostgresEvalLedger
    from cairn.core.storage.knowledge_corpora import PostgresWorkspaceCorpusStore
    from cairn.core.storage.postgres.runs import PostgresRunStore
    from cairn.core.storage.rag_catalog import PostgresRagCatalog

    stores = [
        PostgresRunStore(dsn=DSN),
        PostgresEvalLedger(dsn=DSN),
        PostgresRagCatalog(dsn=DSN),
        PostgresWorkspaceCorpusStore(dsn=DSN),
    ]
    pools = [await s._get_pool() for s in stores]
    assert all(p is pools[0] for p in pools)   # one shared pool object
    assert len(fake_asyncpg) == 1              # opened exactly once (was 4)


# ── #666: the invariant — NO app store opens its own pool ──────────────

def test_no_app_store_opens_its_own_pool():
    """#666 root cause + fix: every app Postgres store must acquire from
    ``shared_pool()`` rather than call ``asyncpg.create_pool`` itself — otherwise
    N stores × M processes overrun ``max_connections`` (live-reproduced with the
    remote-train stack). Only three files are allowed a direct ``create_pool``:
    the shared-pool helper itself, ``pgvector`` (needs a per-connection
    vector-codec ``init=`` hook), and the Postgres secret backend (a per-call pool
    it opens and closes). This is a source-level guard so a new store can't
    silently reintroduce the leak."""
    import pathlib

    pkg = pathlib.Path(__file__).resolve().parents[2] / "src" / "cairn"
    allowed = {
        "core/storage/postgres/pool.py",    # the shared-pool helper
        "services/rag/stores/pgvector.py",  # per-connection vector-codec init hook
        "core/secrets_store.py",            # per-call pool, opened+closed each op
    }
    offenders = []
    for base in ("core", "services"):
        for p in (pkg / base).rglob("*.py"):
            rel = p.relative_to(pkg).as_posix()
            if rel in allowed or "/tests/" in rel:
                continue
            if "asyncpg.create_pool(" in p.read_text(encoding="utf-8"):
                offenders.append(rel)
    assert not offenders, (
        "these stores still open their own asyncpg pool — route them through "
        f"shared_pool(dsn) instead (see #614/#666): {sorted(offenders)}"
    )
