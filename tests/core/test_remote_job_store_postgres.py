"""Postgres backend for the remote-job store (#427 — storage unification).

``remote_jobs`` was sqlite/in-memory-only and ignored ``OBS_STORAGE`` (each
container wrote its own ``~/.cairn/remote_jobs.db``). This gives it a Postgres
backend and makes :func:`get_remote_job_store` honor the single ``OBS_STORAGE``
selector, mirroring the connection/run stores.

Structural tests only (no live PG): (a) a postgres:// URL routes to
:class:`PostgresRemoteJobStore`; (b) OBS_STORAGE unset preserves the existing
OBS_JOB_STORE/sqlite/in-memory behavior unchanged.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.operator.job_store import (
    InMemoryRemoteJobStore,
    PostgresRemoteJobStore,
    SqliteRemoteJobStore,
    get_remote_job_store,
    reset_remote_job_store,
)


@pytest.fixture(autouse=True)
def _reset_store_singleton():
    """The sqlite store is a process-level singleton (_STORE) — reset it
    around each test so one test's cached instance doesn't leak into another."""
    reset_remote_job_store()
    yield
    reset_remote_job_store()


# ─── Lazy instantiation (mirrors PostgresConnectionStore's lazy-pool test) ──


def test_pg_remote_job_store_instantiates_without_dep():
    """Constructor doesn't import asyncpg — the dep stays lazy/optional."""
    s = PostgresRemoteJobStore(dsn="postgres://nowhere/db")
    assert s._pool is None and s._schema_ready is False


@pytest.mark.asyncio
async def test_pg_pool_raises_clearly_without_dsn():
    store = PostgresRemoteJobStore()
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="storage-postgres"):
            await store._get_pool()
    else:
        with pytest.raises(RuntimeError, match="CAIRN_STORAGE=postgres"):
            await store._get_pool()


# ─── (a) OBS_STORAGE postgres:// → PostgresRemoteJobStore ──────────────────


@pytest.mark.parametrize(
    "url",
    ["postgres://u:p@h/db", "postgresql://u:p@h/db"],
)
def test_obs_storage_postgres_routes_to_postgres_job_store(monkeypatch, url):
    monkeypatch.setenv("OBS_STORAGE", url)
    monkeypatch.delenv("OBS_JOB_STORE", raising=False)
    store = get_remote_job_store()
    try:
        assert isinstance(store, PostgresRemoteJobStore)
        assert store._dsn == url
    finally:
        store.close()


def test_obs_storage_postgres_takes_precedence_over_obs_job_store(monkeypatch):
    """When both are set, OBS_STORAGE wins (mirrors default_store's precedence)."""
    monkeypatch.setenv("OBS_STORAGE", "postgres://u:p@h/db")
    monkeypatch.setenv("OBS_JOB_STORE", "memory")
    store = get_remote_job_store()
    try:
        assert isinstance(store, PostgresRemoteJobStore)
    finally:
        store.close()


# ─── (b) OBS_STORAGE unset → existing OBS_JOB_STORE/sqlite/in-memory behavior ──


def test_obs_storage_unset_memory_selector_unchanged(monkeypatch):
    """OBS_JOB_STORE=memory still yields the in-memory store when OBS_STORAGE
    is unset — the pre-existing behavior must not regress."""
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    monkeypatch.setenv("OBS_JOB_STORE", "memory")
    assert isinstance(get_remote_job_store(), InMemoryRemoteJobStore)


def test_obs_storage_unset_default_sqlite_unchanged(monkeypatch, tmp_path):
    """OBS_STORAGE unset + OBS_JOB_STORE unset → durable sqlite default,
    same as before #427."""
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    monkeypatch.delenv("OBS_JOB_STORE", raising=False)
    monkeypatch.setenv("OBS_JOB_STORE_DB", str(tmp_path / "jobs.db"))
    store = get_remote_job_store()
    try:
        assert isinstance(store, SqliteRemoteJobStore)
    finally:
        store.close()


def test_obs_storage_unrecognized_scheme_falls_back(monkeypatch, tmp_path):
    """A non-postgres OBS_STORAGE value doesn't change the legacy routing —
    falls through to OBS_JOB_STORE handling."""
    monkeypatch.setenv("OBS_STORAGE", "sqlite:///tmp/x.db")
    monkeypatch.setenv("OBS_JOB_STORE", "memory")
    assert isinstance(get_remote_job_store(), InMemoryRemoteJobStore)


# ─── Live roundtrip (gated on PGSTORE_DSN + dep; not run here) ─────────────


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
async def test_live_remote_job_store_roundtrip(_pg_dsn):
    from cairn.core.operator.job_store import DONE, RemoteJobRecord

    store = PostgresRemoteJobStore(dsn=_pg_dsn)
    try:
        rec = RemoteJobRecord(run_id="run-pg-1", node_name="train", provider="runpod", job_id="rp-1", backend="remote_job")
        await store.register(rec)
        assert (await store.get(rec.id)).run_id == "run-pg-1"
        active = await store.claim_active()
        assert any(r.id == rec.id for r in active)
        await store.mark_running(rec.id)
        assert (await store.get(rec.id)).status == "running"
        await store.mark_done(rec.id, {"adapter_uri": "s3://b/a"})
        got = await store.get(rec.id)
        assert got.status == DONE and got.result == {"adapter_uri": "s3://b/a"}
        assert await store.claim_active() == [] or all(r.id != rec.id for r in await store.claim_active())
        listed = await store.list(workspace_id=rec.workspace_id)
        assert any(r.id == rec.id for r in listed)
    finally:
        store.close()
