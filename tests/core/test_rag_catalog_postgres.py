"""Postgres backend for the RAG catalog store (#427 — storage unification).

``RagCatalogStore`` was sqlite/in-memory-only and ignored ``OBS_STORAGE``. This
gives it a Postgres backend and makes ``default_rag_catalog()`` honor the
single ``OBS_STORAGE`` selector, mirroring the connection store + run store.

Structural tests only (no live PG): construction stays lazy (no asyncpg import
on instantiation), and the env-routing dispatch resolves to the right TYPE.
Mirrors ``tests/core/test_connections_pg_store.py``.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.storage.rag_catalog import (
    InMemoryRagCatalog,
    PostgresRagCatalog,
    SqliteRagCatalog,
    default_rag_catalog,
)


# ─── Lazy instantiation + DSN guard ────────────────────────────────────

def test_pg_rag_catalog_instantiates_without_dep():
    """Constructor doesn't import asyncpg — the dep stays lazy/optional."""
    s = PostgresRagCatalog(dsn="postgres://nowhere/db")
    assert s._pool is None and s._schema_ready is False


@pytest.mark.asyncio
async def test_pg_pool_raises_clearly_without_dsn():
    store = PostgresRagCatalog()
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="storage-postgres"):
            await store._get_pool()
    else:
        with pytest.raises(RuntimeError, match="CAIRN_STORAGE=postgres"):
            await store._get_pool()


# ─── OBS_STORAGE / OBS_RAG_CATALOG dispatch ────────────────────────────

def test_obs_storage_postgres_routes_to_postgres(monkeypatch):
    """postgres:// / postgresql:// scheme → PostgresRagCatalog (lazy pool)."""
    monkeypatch.setenv("OBS_STORAGE", "postgres://user:pass@localhost/db")
    monkeypatch.delenv("OBS_RAG_CATALOG", raising=False)
    s = default_rag_catalog()
    assert isinstance(s, PostgresRagCatalog)
    assert s._dsn == "postgres://user:pass@localhost/db"


def test_obs_storage_postgresql_scheme_also_accepted(monkeypatch):
    monkeypatch.setenv("OBS_STORAGE", "postgresql://x/y")
    monkeypatch.delenv("OBS_RAG_CATALOG", raising=False)
    assert isinstance(default_rag_catalog(), PostgresRagCatalog)


def test_obs_storage_unset_and_no_rag_catalog_env_is_in_memory(monkeypatch):
    """Neither knob set → volatile InMemoryRagCatalog (historical default)."""
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    monkeypatch.delenv("OBS_RAG_CATALOG", raising=False)
    s = default_rag_catalog()
    assert isinstance(s, InMemoryRagCatalog)


def test_obs_rag_catalog_path_still_routes_to_sqlite(monkeypatch, tmp_path):
    """Existing opt-in OBS_RAG_CATALOG path behavior is unchanged when
    OBS_STORAGE isn't a postgres URL."""
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    db_path = str(tmp_path / "rag_catalog.db")
    monkeypatch.setenv("OBS_RAG_CATALOG", db_path)
    s = default_rag_catalog()
    assert isinstance(s, SqliteRagCatalog)
    assert s.path == db_path


def test_obs_storage_takes_precedence_over_obs_rag_catalog(monkeypatch, tmp_path):
    """When both are set, OBS_STORAGE (postgres) wins over OBS_RAG_CATALOG."""
    monkeypatch.setenv("OBS_STORAGE", "postgres://x/y")
    monkeypatch.setenv("OBS_RAG_CATALOG", str(tmp_path / "rag_catalog.db"))
    s = default_rag_catalog()
    assert isinstance(s, PostgresRagCatalog)


def test_obs_storage_unknown_scheme_falls_back_to_existing_behavior(monkeypatch, tmp_path):
    """Unrecognized OBS_STORAGE scheme falls back to the OBS_RAG_CATALOG /
    in-memory behavior, same as the run store / connection store fallback."""
    monkeypatch.setenv("OBS_STORAGE", "redis://something")
    monkeypatch.delenv("OBS_RAG_CATALOG", raising=False)
    s = default_rag_catalog()
    assert isinstance(s, InMemoryRagCatalog)
