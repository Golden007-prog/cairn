"""Postgres backend for the policy store (#427 — storage unification).

`policies` was sqlite-only and ignored `OBS_STORAGE` (each container wrote its
own ~/.cairn/policies.db). This gives it a Postgres backend and makes
`default_policy_store()` honor the single `OBS_STORAGE` selector, mirroring the
registry-family stores (see test_connections_pg_store.py).

Structural tests always run (no live PG); the live roundtrip is gated on
`PGSTORE_DSN` + the `storage-postgres` dep.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.policy.store import (
    InMemoryPolicyStore,
    PostgresPolicyStore,
    SqlitePolicyStore,
    StoredPolicy,
    default_policy_store,
)


# ─── Lazy instantiation + DSN guard ───────────────────────────────────

def test_pg_policy_store_instantiates_without_dep():
    """Constructor doesn't import asyncpg — the dep stays lazy/optional."""
    s = PostgresPolicyStore(dsn="postgres://nowhere/db")
    assert s._pool is None and s._schema_ready is False


@pytest.mark.asyncio
async def test_pg_pool_raises_clearly_without_dsn():
    store = PostgresPolicyStore()
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
        ("postgres://u:p@h/db", PostgresPolicyStore),
        ("postgresql://u:p@h/db", PostgresPolicyStore),
        ("sqlite:///tmp/x.db", SqlitePolicyStore),
        ("", SqlitePolicyStore),
    ],
)
def test_factory_honors_obs_storage(monkeypatch, tmp_path, url, expect):
    monkeypatch.setenv("OBS_STORAGE", url)
    monkeypatch.setenv("OBS_POLICIES_DB", str(tmp_path / "pol.db"))
    store = default_policy_store()
    try:
        assert isinstance(store, expect)
    finally:
        store.close()


def test_factory_memory_override(monkeypatch):
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    monkeypatch.setenv("OBS_POLICIES_DB", ":memory:")
    assert isinstance(default_policy_store(), InMemoryPolicyStore)


def test_factory_sqlite_when_nothing_set(monkeypatch, tmp_path):
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    monkeypatch.setenv("OBS_POLICIES_DB", str(tmp_path / "pol.db"))
    assert isinstance(default_policy_store(), SqlitePolicyStore)


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
async def test_live_policy_store_roundtrip(_pg_dsn):
    store = PostgresPolicyStore(dsn=_pg_dsn)
    try:
        p = StoredPolicy(name="pg-guardrail", yaml="rules: []")
        await store.create(p)
        assert (await store.get(p.id)).name == "pg-guardrail"
        assert (await store.get_by_name("pg-guardrail")).id == p.id
        assert any(x.name == "pg-guardrail" for x in await store.list())
        assert any(x.name == "pg-guardrail" for x in store.list_sync())
        assert await store.delete(p.id) is True
        assert await store.get(p.id) is None
    finally:
        store.close()
