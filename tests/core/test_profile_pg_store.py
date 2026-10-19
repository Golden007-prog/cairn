"""Postgres backend for the profile store (#427 — storage unification).

`profiles` was sqlite-only and ignored `OBS_STORAGE`. This gives it a Postgres
backend and makes `default_profile_store()` honor the single `OBS_STORAGE`
selector, mirroring the registry-family stores (see test_connections_pg_store.py).

Structural tests always run (no live PG); the live roundtrip is gated on
`PGSTORE_DSN` + the `storage-postgres` dep.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.profile_store import (
    InMemoryProfileStore,
    PostgresProfileStore,
    ProfileRecord,
    SqliteProfileStore,
    default_profile_store,
)


# ─── Lazy instantiation + DSN guard ───────────────────────────────────

def test_pg_profile_store_instantiates_without_dep():
    """Constructor doesn't import asyncpg — the dep stays lazy/optional."""
    s = PostgresProfileStore(dsn="postgres://nowhere/db")
    assert s._pool is None and s._schema_ready is False


@pytest.mark.asyncio
async def test_pg_pool_raises_clearly_without_dsn():
    store = PostgresProfileStore()
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
        ("postgres://u:p@h/db", PostgresProfileStore),
        ("postgresql://u:p@h/db", PostgresProfileStore),
        ("sqlite:///tmp/x.db", SqliteProfileStore),
    ],
)
def test_factory_honors_obs_storage(monkeypatch, tmp_path, url, expect):
    monkeypatch.setenv("OBS_STORAGE", url)
    monkeypatch.setenv("OBS_PROFILES_DB", str(tmp_path / "prof.db"))
    store = default_profile_store()
    try:
        assert isinstance(store, expect)
    finally:
        store.close()


def test_factory_inmemory_when_nothing_set(monkeypatch):
    """profiles is in-memory unless OBS_PROFILES_DB set (pre-existing behavior)."""
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    monkeypatch.delenv("OBS_PROFILES_DB", raising=False)
    assert isinstance(default_profile_store(), InMemoryProfileStore)


def test_factory_sqlite_when_profiles_db_set(monkeypatch, tmp_path):
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    monkeypatch.setenv("OBS_PROFILES_DB", str(tmp_path / "prof.db"))
    assert isinstance(default_profile_store(), SqliteProfileStore)


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
async def test_live_profile_store_roundtrip(_pg_dsn):
    store = PostgresProfileStore(dsn=_pg_dsn)
    try:
        p = ProfileRecord(name="pg-profile", bindings={"llm": "openai"})
        await store.create(p)
        assert (await store.get(p.id)).name == "pg-profile"
        await store.update(p.id, description="tuned")
        assert (await store.get(p.id)).description == "tuned"
        assert any(x.name == "pg-profile" for x in await store.list())
        assert await store.delete(p.id) is True
        assert await store.get(p.id) is None
    finally:
        store.close()
