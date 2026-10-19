"""Postgres backend for the connection store (#427 — storage unification).

`connections` was sqlite-only and ignored `OBS_STORAGE` (each container wrote
its own ~/.cairn/connections.db → inconsistent + ephemeral on redeploy). This
gives it a Postgres backend and makes `default_connection_store()` honor the
single `OBS_STORAGE` selector, mirroring the registry-family stores.

Structural tests always run (no live PG); the live roundtrip is gated on
`PGSTORE_DSN` + the `storage-postgres` dep.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.connections import (
    Connection,
    InMemoryConnectionStore,
    PostgresConnectionStore,
    SqliteConnectionStore,
    default_connection_store,
)


# ─── Lazy instantiation + DSN guard ───────────────────────────────────

def test_pg_connection_store_instantiates_without_dep():
    """Constructor doesn't import asyncpg — the dep stays lazy/optional."""
    s = PostgresConnectionStore(dsn="postgres://nowhere/db")
    assert s._pool is None and s._schema_ready is False


@pytest.mark.asyncio
async def test_pg_pool_raises_clearly_without_dsn():
    store = PostgresConnectionStore()
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="storage-postgres"):
            await store._get_pool()
    else:
        with pytest.raises(RuntimeError, match="CAIRN_STORAGE=postgres"):
            await store._get_pool()


# ─── OBS_STORAGE dispatch (the #427 core: the factory honors the URL) ──

@pytest.mark.parametrize(
    "url,expect",
    [
        ("postgres://u:p@h/db", PostgresConnectionStore),
        ("postgresql://u:p@h/db", PostgresConnectionStore),
        ("sqlite:///tmp/x.db", SqliteConnectionStore),
        ("", SqliteConnectionStore),
    ],
)
def test_factory_honors_obs_storage(monkeypatch, tmp_path, url, expect):
    monkeypatch.setenv("OBS_STORAGE", url)
    monkeypatch.setenv("OBS_CONNECTIONS_DB", str(tmp_path / "conn.db"))
    store = default_connection_store()
    try:
        assert isinstance(store, expect)
    finally:
        store.close()


def test_factory_memory_override(monkeypatch):
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    monkeypatch.setenv("OBS_CONNECTIONS_DB", ":memory:")
    assert isinstance(default_connection_store(), InMemoryConnectionStore)


# ─── Live roundtrip (gated on PGSTORE_DSN + dep) ───────────────────────

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
async def test_live_connection_store_roundtrip(_pg_dsn):
    store = PostgresConnectionStore(dsn=_pg_dsn)
    try:
        c = Connection(name="pg-openai", kind="api_key", secrets={"API_KEY": "OPENAI_API_KEY"})
        await store.create(c)
        assert (await store.get(c.id)).name == "pg-openai"
        assert (await store.get_by_name("pg-openai")).id == c.id
        await store.update(c.id, kind="http")
        assert (await store.get(c.id)).kind == "http"
        assert any(x.name == "pg-openai" for x in await store.list())
        assert await store.delete(c.id) is True
        assert await store.get(c.id) is None
    finally:
        store.close()
