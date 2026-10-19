"""Postgres backend for the install store (#427 — storage unification).

`installs` was sqlite-only and ignored `OBS_STORAGE`. This gives it a Postgres
backend and makes `get_install_store()` honor the single `OBS_STORAGE` selector,
mirroring the registry-family stores (see test_connections_pg_store.py).

Structural tests always run (no live PG); the live roundtrip is gated on
`PGSTORE_DSN` + the `storage-postgres` dep.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

import cairn.core.registry_install as ri
from cairn.core.registry_install import (
    InstallRecord,
    PostgresInstallStore,
    SqliteInstallStore,
    get_install_store,
)


# ─── Lazy instantiation + DSN guard ───────────────────────────────────

def test_pg_install_store_instantiates_without_dep():
    """Constructor doesn't import asyncpg — the dep stays lazy/optional."""
    s = PostgresInstallStore(dsn="postgres://nowhere/db")
    assert s._pool is None and s._schema_ready is False


@pytest.mark.asyncio
async def test_pg_pool_raises_clearly_without_dsn():
    store = PostgresInstallStore()
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
        ("postgres://u:p@h/db", PostgresInstallStore),
        ("postgresql://u:p@h/db", PostgresInstallStore),
        ("sqlite:///tmp/x.db", SqliteInstallStore),
        ("", SqliteInstallStore),
    ],
)
def test_factory_honors_obs_storage(monkeypatch, tmp_path, url, expect):
    monkeypatch.setenv("OBS_STORAGE", url)
    monkeypatch.setenv("OBS_INSTALLS_DB", str(tmp_path / "installs.db"))
    monkeypatch.setattr(ri, "_store", None)  # force a fresh dispatch (cached singleton)
    try:
        assert isinstance(get_install_store(), expect)
    finally:
        monkeypatch.setattr(ri, "_store", None)


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
async def test_live_install_store_roundtrip(_pg_dsn):
    store = PostgresInstallStore(dsn=_pg_dsn)
    try:
        r = InstallRecord(ref="acme/pack", kind="operator_pack", status="installed")
        await store.upsert(r)
        assert (await store.get("acme/pack")).status == "installed"
        # upsert-in-place on same id
        r.status = "removed"
        await store.upsert(r)
        assert (await store.get("acme/pack")).status == "removed"
        assert any(x.ref == "acme/pack" for x in await store.list())
    finally:
        store.close()
