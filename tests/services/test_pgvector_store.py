"""Structural tests for the pgvector VectorStore driver.

Without a live Postgres + pgvector extension, we can only verify:

  1. The driver imports and instantiates without the optional dep group
     installed (it stays lazy until first call).
  2. Collection-name validation rejects SQL-injection footguns.
  3. Without ``PGVECTOR_DSN`` set, the lazy pool raises a clear error.
  4. The SQL helper functions produce the expected shape.

Live integration is gated on ``PGVECTOR_DSN`` being set (skipped in CI).
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.services.rag.stores.pgvector import (
    PGVectorStore,
    _create_index_sql,
    _create_source_index_sql,
    _create_table_sql,
    _delete_by_source_sql,
    _has_source_sql,
    _list_sources_sql,
    _search_sql,
    _upsert_sql,
)


# ─── Instantiation + validation ───────────────────────────────────────


def test_pgvector_store_instantiates_without_dep_installed():
    """Constructor doesn't import asyncpg — the dep stays lazy."""
    store = PGVectorStore()
    assert store._pool is None
    assert store._collection is None


def test_safe_collection_accepts_valid_names():
    assert PGVectorStore._safe_collection("runbooks") == "runbooks"
    assert PGVectorStore._safe_collection("incident_pack_v2") == "incident_pack_v2"
    assert PGVectorStore._safe_collection("col_123") == "col_123"


def test_safe_collection_rejects_injection_attempts():
    """The validator is the only thing standing between user input
    and a footgun. Reject anything that isn't alphanumeric+underscore."""
    bad = [
        "drop_table; --",
        "runbooks-v2",          # hyphen
        "runbooks v2",          # space
        "runbooks';DROP--",     # injection
        "runbooks.public",      # dot
        "",                     # empty
    ]
    for name in bad:
        with pytest.raises(ValueError, match="invalid collection name"):
            PGVectorStore._safe_collection(name)


# ─── SQL shape ────────────────────────────────────────────────────────


def test_create_table_sql_includes_vector_dim():
    sql = _create_table_sql("incident_runbooks", 1024)
    assert "incident_runbooks" in sql
    assert "vector(1024)" in sql
    assert "id        TEXT PRIMARY KEY" in sql
    assert "embedding vector(1024) NOT NULL" in sql
    assert "payload   JSONB" in sql


def test_create_index_sql_uses_hnsw():
    """HNSW > IVFFlat for typical recall at small-to-mid scale."""
    sql = _create_index_sql("runbooks")
    assert "USING hnsw" in sql
    assert "vector_cosine_ops" in sql


def test_upsert_sql_uses_on_conflict():
    sql = _upsert_sql("runbooks")
    assert "INSERT INTO runbooks" in sql
    assert "ON CONFLICT (id) DO UPDATE" in sql
    assert "$1" in sql and "$2" in sql and "$3" in sql and "$4" in sql


def test_search_sql_uses_cosine_distance_order():
    """`<=>` is cosine distance — smaller is closer. ORDER ASC."""
    sql = _search_sql("runbooks")
    assert "embedding <=> $1" in sql
    assert "ORDER BY embedding <=> $1" in sql
    assert "LIMIT $2" in sql
    assert "1 - (embedding <=> $1) AS score" in sql


def test_search_sql_with_filter():
    sql = _search_sql("runbooks", "payload @> $3::jsonb")
    assert "WHERE payload @> $3::jsonb" in sql


def test_delete_by_source_sql():
    sql = _delete_by_source_sql("runbooks")
    assert "DELETE FROM runbooks WHERE source = $1" in sql


def test_has_source_sql_limits_to_one():
    sql = _has_source_sql("runbooks")
    assert "LIMIT 1" in sql


def test_list_sources_sql_groups_and_counts():
    sql = _list_sources_sql("runbooks")
    assert "GROUP BY source" in sql
    assert "COUNT(*)" in sql
    assert "source_type" in sql


def test_create_source_index_for_filter_paths():
    sql = _create_source_index_sql("runbooks")
    assert "ON runbooks(source)" in sql


# ─── Lazy-pool error path ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_pool_raises_when_dsn_missing(monkeypatch):
    """If asyncpg + pgvector are installed but PGVECTOR_DSN is unset,
    the lazy pool surfaces a clear error before opening a connection."""
    # Skip if the optional dep group isn't installed — there's no way
    # to test this path without it.
    try:
        import asyncpg  # noqa: F401
        from pgvector.asyncpg import register_vector  # noqa: F401
    except ImportError:
        pytest.skip("rag-pgvector optional dep group not installed")

    monkeypatch.setattr(
        "cairn.core.provider_settings.get_settings",
        lambda: type("S", (), {"pgvector_dsn": ""})(),
    )
    store = PGVectorStore()
    with pytest.raises(RuntimeError, match="PGVECTOR_DSN is not set"):
        await store._get_pool()


@pytest.mark.asyncio
async def test_get_pool_raises_clearly_when_dep_missing(monkeypatch):
    """If the rag-pgvector dep group isn't installed, the error message
    points the operator at the right install command."""
    try:
        import asyncpg  # noqa: F401
        import pgvector  # noqa: F401
    except ImportError:
        # Dep not installed → the error fires from _require_asyncpg.
        store = PGVectorStore()
        with pytest.raises(RuntimeError, match="rag-pgvector"):
            await store._get_pool()
        return
    # Dep installed → can't test the "missing dep" branch in this env.
    pytest.skip("rag-pgvector is installed; can't test missing-dep branch here")
