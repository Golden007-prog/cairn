"""Postgres backend for the schedule store (#427 — storage unification).

`schedules` was sqlite-only and ignored `OBS_STORAGE`. This gives it a Postgres
backend (indexed `workspace_id`, ADR-0051) and makes `default_schedule_store()`
honor the single `OBS_STORAGE` selector, mirroring the registry-family stores
(see test_connections_pg_store.py / test_workspace_pg_stores.py).

Structural tests always run (no live PG); the live roundtrip is gated on
`PGSTORE_DSN` + the `storage-postgres` dep.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.storage.schedule_store import (
    InMemoryScheduleStore,
    PostgresScheduleStore,
    SqliteScheduleStore,
    StoredSchedule,
    default_schedule_store,
)


# ─── Lazy instantiation + DSN guard ───────────────────────────────────

def test_pg_schedule_store_instantiates_without_dep():
    """Constructor doesn't import asyncpg — the dep stays lazy/optional."""
    s = PostgresScheduleStore(dsn="postgres://nowhere/db")
    assert s._pool is None and s._schema_ready is False


@pytest.mark.asyncio
async def test_pg_pool_raises_clearly_without_dsn():
    store = PostgresScheduleStore()
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
        ("postgres://u:p@h/db", PostgresScheduleStore),
        ("postgresql://u:p@h/db", PostgresScheduleStore),
        ("sqlite:///tmp/x.db", SqliteScheduleStore),
        ("", SqliteScheduleStore),
    ],
)
def test_factory_honors_obs_storage(monkeypatch, tmp_path, url, expect):
    monkeypatch.setenv("OBS_STORAGE", url)
    monkeypatch.setenv("OBS_SCHEDULES_DB", str(tmp_path / "sched.db"))
    store = default_schedule_store()
    try:
        assert isinstance(store, expect)
    finally:
        store.close()


def test_factory_memory_override(monkeypatch):
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    monkeypatch.setenv("OBS_SCHEDULES_DB", ":memory:")
    assert isinstance(default_schedule_store(), InMemoryScheduleStore)


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
async def test_live_schedule_store_roundtrip_and_scoping(_pg_dsn):
    store = PostgresScheduleStore(dsn=_pg_dsn)
    try:
        a = StoredSchedule(name="pg-nightly-a", cron="0 0 * * *", workflow="wf/x", workspace_id="pgws-a")
        b = StoredSchedule(name="pg-nightly-b", cron="0 1 * * *", workflow="wf/y", workspace_id="pgws-b")
        await store.create(a)
        await store.create(b)

        # workspace-scoped get / list use the indexed workspace_id column
        assert (await store.get(a.id, "pgws-a")) is not None
        assert (await store.get(a.id, "pgws-b")) is None
        assert {s.id for s in await store.list("pgws-a")} == {a.id}
        assert {s.id for s in store.list_sync("pgws-a")} == {a.id}

        # set_enabled toggles + persists
        toggled = await store.set_enabled(a.id, False, "pgws-a")
        assert toggled is not None and toggled.enabled is False
        assert (await store.get(a.id, "pgws-a")).enabled is False

        # scoped delete
        assert await store.delete(a.id, "pgws-b") is False  # wrong workspace
        assert await store.delete(a.id, "pgws-a") is True
        assert await store.get(a.id, "pgws-a") is None
        assert await store.delete(b.id) is True
    finally:
        store.close()
