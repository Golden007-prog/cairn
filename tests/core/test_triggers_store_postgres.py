"""Postgres backend for the trigger store (#427 — storage unification).

`triggers` was sqlite-only and ignored `OBS_STORAGE` (each container wrote its
own ~/.cairn/triggers.db → inconsistent + ephemeral on redeploy). This gives
it a Postgres backend and makes `default_trigger_store()` honor the single
`OBS_STORAGE` selector, mirroring the registry-family stores
(`cairn.core.connections.PostgresConnectionStore` / `default_connection_store`).

Structural tests always run (no live PG); the live roundtrip is gated on
`PGSTORE_DSN` + the `storage-postgres` dep.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.storage.trigger_store import (
    InMemoryTriggerStore,
    PostgresTriggerStore,
    SqliteTriggerStore,
    StoredTrigger,
    default_trigger_store,
)

TRIGGER_YAML = """
source: pg-test
description: created for the postgres store test
ingress:
  transport: http
  path: /triggers/pg-test
  method: POST
normalize:
  event_type: status
  raw: .
"""


# ─── Lazy instantiation + DSN guard ───────────────────────────────────

def test_pg_trigger_store_instantiates_without_dep():
    """Constructor doesn't import asyncpg — the dep stays lazy/optional."""
    s = PostgresTriggerStore(dsn="postgres://nowhere/db")
    assert s._pool is None and s._schema_ready is False


@pytest.mark.asyncio
async def test_pg_pool_raises_clearly_without_dsn():
    store = PostgresTriggerStore()
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="storage-postgres"):
            await store._get_pool()
    else:
        with pytest.raises(RuntimeError, match="CAIRN_STORAGE=postgres"):
            await store._get_pool()


# ─── OBS_STORAGE dispatch (the #427 core: the factory honors the URL) ──

def test_default_trigger_store_returns_postgres_when_obs_storage_postgres(monkeypatch):
    """(a) OBS_STORAGE=postgres://x → PostgresTriggerStore, DSN passed through."""
    monkeypatch.setenv("OBS_STORAGE", "postgres://user:pass@localhost/db")
    s = default_trigger_store()
    assert isinstance(s, PostgresTriggerStore)
    assert s._dsn == "postgres://user:pass@localhost/db"


def test_default_trigger_store_returns_postgres_for_postgresql_scheme(monkeypatch):
    monkeypatch.setenv("OBS_STORAGE", "postgresql://u:p@h/db")
    assert isinstance(default_trigger_store(), PostgresTriggerStore)


@pytest.mark.parametrize(
    "url,expect",
    [
        ("postgres://u:p@h/db", PostgresTriggerStore),
        ("postgresql://u:p@h/db", PostgresTriggerStore),
        ("sqlite:///tmp/x.db", SqliteTriggerStore),
        ("", SqliteTriggerStore),
    ],
)
def test_factory_honors_obs_storage(monkeypatch, tmp_path, url, expect):
    monkeypatch.setenv("OBS_STORAGE", url)
    monkeypatch.setenv("OBS_TRIGGERS_DB", str(tmp_path / "trig.db"))
    store = default_trigger_store()
    try:
        assert isinstance(store, expect)
    finally:
        store.close()


def test_default_trigger_store_returns_sqlite_when_unset(monkeypatch, tmp_path):
    """(b) OBS_STORAGE unset → the existing sqlite store (unchanged behavior)."""
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    monkeypatch.setenv("OBS_TRIGGERS_DB", str(tmp_path / "trig.db"))
    s = default_trigger_store()
    try:
        assert isinstance(s, SqliteTriggerStore)
    finally:
        s.close()


def test_default_trigger_store_memory_override(monkeypatch):
    """OBS_TRIGGERS_DB=:memory: still opts into the volatile in-memory store
    when OBS_STORAGE isn't a postgres URL — preserves current behavior."""
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    monkeypatch.setenv("OBS_TRIGGERS_DB", ":memory:")
    assert isinstance(default_trigger_store(), InMemoryTriggerStore)


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
async def test_live_trigger_store_roundtrip(_pg_dsn):
    store = PostgresTriggerStore(dsn=_pg_dsn)
    try:
        t = StoredTrigger(name="pg-trigger-test", source="pg-test", yaml=TRIGGER_YAML)
        await store.create(t)
        assert (await store.get(t.id)).name == "pg-trigger-test"
        assert (await store.get_by_name("pg-trigger-test")).id == t.id
        assert any(x.name == "pg-trigger-test" for x in await store.list())
        assert any(x.name == "pg-trigger-test" for x in await store.list(workspace_id=t.workspace_id))
        assert await store.delete(t.id) is True
        assert await store.get(t.id) is None
    finally:
        store.close()
