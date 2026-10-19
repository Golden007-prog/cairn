"""Postgres backend for the OAuth token store (#427 — storage unification).

`oauth_tokens` / `oauth_pending` were sqlite-only and ignored `OBS_STORAGE`.
This gives them a Postgres backend and makes `default_token_store()` honor the
single `OBS_STORAGE` selector, mirroring the registry-family stores (see
test_connections_pg_store.py).

Structural tests always run (no live PG); the live roundtrip is gated on
`PGSTORE_DSN` + the `storage-postgres` dep.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.oauth.store import (
    OAuthTokenStore,
    PostgresOAuthTokenStore,
    default_token_store,
    reset_token_store,
)


# ─── Lazy instantiation + DSN guard ───────────────────────────────────

def test_pg_oauth_store_instantiates_without_dep():
    """Constructor doesn't import asyncpg — the dep stays lazy/optional."""
    s = PostgresOAuthTokenStore(dsn="postgres://nowhere/db")
    assert s._pool is None and s._schema_ready is False


@pytest.mark.asyncio
async def test_pg_pool_raises_clearly_without_dsn():
    store = PostgresOAuthTokenStore()
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
        ("postgres://u:p@h/db", PostgresOAuthTokenStore),
        ("postgresql://u:p@h/db", PostgresOAuthTokenStore),
        ("sqlite:///tmp/x.db", OAuthTokenStore),
        ("", OAuthTokenStore),
    ],
)
def test_factory_honors_obs_storage(monkeypatch, tmp_path, url, expect):
    monkeypatch.setenv("OBS_STORAGE", url)
    monkeypatch.setenv("OBS_OAUTH_TOKEN_DB", str(tmp_path / "oauth.db"))
    reset_token_store()
    try:
        assert isinstance(default_token_store(), expect)
    finally:
        reset_token_store()


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
async def test_live_oauth_store_roundtrip(_pg_dsn):
    store = PostgresOAuthTokenStore(dsn=_pg_dsn)
    try:
        # pending authorization: put then pop (atomic delete-returning)
        await store.put_pending("state-xyz", {"provider": "github", "verifier": "v1"})
        popped = await store.pop_pending("state-xyz")
        assert popped == {"provider": "github", "verifier": "v1"}
        assert await store.pop_pending("state-xyz") is None  # consumed

        # tokens: put / get / delete keyed by connection_id
        await store.put_token("conn-1", "github", {"access_token": "at", "refresh_token": "rt"})
        got = await store.get_token("conn-1")
        assert got is not None and got["access_token"] == "at"
        assert await store.delete_token("conn-1") is True
        assert await store.get_token("conn-1") is None
    finally:
        store.close()
