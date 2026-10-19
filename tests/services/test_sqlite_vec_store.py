"""Structural tests for the sqlite-vec VectorStore driver.

Without the optional ``rag-sqlite-vec`` dep group installed we still
verify:

  1. The driver imports + instantiates lazily (no sqlite-vec import on
     construction).
  2. Collection-name validation rejects SQL-injection footguns
     (table names are interpolated; the validator is the defense).
  3. Default-path resolution honors precedence:
        OBS_SQLITE_VEC_DB > OBS_RUN_DB > ~/.cairn/runs.db.
  4. ``_serialize_float32`` produces the correct little-endian float32
     bytes for the vec0 wire format.
  5. SQL helper functions produce the expected shapes (two-table
     design, JOIN on rowid, ``WHERE embedding MATCH ? AND v.k = ?``).
  6. Lazy connection raises a clear error when the dep group isn't
     installed.

End-to-end roundtrip (initialize → upsert → search → delete) is gated
on the dep group being importable; skipped otherwise.
"""
from __future__ import annotations

import os
import struct

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.services.rag.stores.sqlite_vec import (
    SqliteVecStore,
    _create_companion_table_sql,
    _create_source_index_sql,
    _create_vec_table_sql,
    _default_db_path,
    _delete_by_source_companion_sql,
    _delete_by_source_select_sql,
    _delete_by_source_vec_sql,
    _delete_companion_sql,
    _delete_vec_sql,
    _has_source_sql,
    _insert_companion_sql,
    _insert_vec_sql,
    _list_sources_sql,
    _next_rowid_sql,
    _search_sql,
    _select_rowid_sql,
    _serialize_float32,
)


# ─── Instantiation + validation ───────────────────────────────────────


def test_sqlite_vec_store_instantiates_without_dep_installed():
    """Constructor doesn't import sqlite_vec — the dep stays lazy."""
    store = SqliteVecStore()
    assert store._conn is None
    assert store._collection is None


def test_safe_collection_accepts_valid_names():
    assert SqliteVecStore._safe_collection("runbooks") == "runbooks"
    assert SqliteVecStore._safe_collection("incident_pack_v2") == "incident_pack_v2"
    assert SqliteVecStore._safe_collection("col_123") == "col_123"


def test_safe_collection_rejects_injection_attempts():
    bad = [
        "drop_table; --",
        "runbooks-v2",
        "runbooks v2",
        "runbooks';DROP--",
        "runbooks.public",
        "",
    ]
    for name in bad:
        with pytest.raises(ValueError, match="invalid collection name"):
            SqliteVecStore._safe_collection(name)


# ─── Default DB path resolution ───────────────────────────────────────


def test_default_db_path_uses_explicit_override(monkeypatch):
    """``OBS_SQLITE_VEC_DB`` takes precedence over everything else."""
    monkeypatch.setenv("OBS_SQLITE_VEC_DB", "/tmp/vec-override.db")
    monkeypatch.setenv("OBS_RUN_DB", "/tmp/runs.db")  # should be shadowed
    assert _default_db_path() == "/tmp/vec-override.db"


def test_default_db_path_falls_back_to_run_db(monkeypatch):
    """Without OBS_SQLITE_VEC_DB, share the SqliteRunStore file —
    the §24.4 Tier-1 "one file" promise."""
    monkeypatch.delenv("OBS_SQLITE_VEC_DB", raising=False)
    monkeypatch.setenv("OBS_RUN_DB", "/tmp/shared-runs.db")
    assert _default_db_path() == "/tmp/shared-runs.db"


def test_default_db_path_final_fallback(monkeypatch):
    """No env set → ``~/.cairn/runs.db`` (matches SqliteRunStore default)."""
    monkeypatch.delenv("OBS_SQLITE_VEC_DB", raising=False)
    monkeypatch.delenv("OBS_RUN_DB", raising=False)
    path = _default_db_path()
    assert path.endswith(".cairn/runs.db")


# ─── Float32 wire format ──────────────────────────────────────────────


def test_serialize_float32_packs_little_endian():
    """sqlite-vec stores vectors as packed little-endian float32."""
    vec = [1.0, 2.0, 3.5]
    blob = _serialize_float32(vec)
    assert len(blob) == 12  # 3 floats × 4 bytes
    # Decode to verify shape
    unpacked = struct.unpack("<3f", blob)
    assert unpacked == pytest.approx((1.0, 2.0, 3.5))


def test_serialize_float32_zero_length():
    """Empty vector returns empty bytes (degenerate but well-defined)."""
    assert _serialize_float32([]) == b""


# ─── SQL shape ────────────────────────────────────────────────────────


def test_create_vec_table_uses_vec0_virtual():
    """The vec0 virtual table is the sqlite-vec contract for fixed-dim
    float32 vectors."""
    sql = _create_vec_table_sql("runbooks", 1024)
    assert "CREATE VIRTUAL TABLE" in sql
    assert "runbooks_vec" in sql
    assert "USING vec0" in sql
    assert "FLOAT[1024]" in sql


def test_create_companion_table_has_rowid_unique():
    """Companion holds external string id + maps to internal int rowid.
    rowid UNIQUE so JOIN back to vec0 is single-row."""
    sql = _create_companion_table_sql("runbooks")
    assert "CREATE TABLE IF NOT EXISTS runbooks" in sql
    assert "id      TEXT PRIMARY KEY" in sql
    assert "source  TEXT" in sql
    assert "payload TEXT" in sql
    assert "rowid   INTEGER UNIQUE" in sql


def test_create_source_index_backs_filter_paths():
    sql = _create_source_index_sql("runbooks")
    assert "ON runbooks(source)" in sql


def test_select_rowid_by_id():
    sql = _select_rowid_sql("runbooks")
    assert "SELECT rowid FROM runbooks WHERE id = ?" in sql


def test_next_rowid_handles_empty_table():
    """COALESCE makes the first insert work without a special case."""
    sql = _next_rowid_sql("runbooks")
    assert "COALESCE(MAX(rowid), 0) + 1" in sql


def test_delete_helpers_target_correct_tables():
    assert "DELETE FROM runbooks WHERE id = ?" in _delete_companion_sql("runbooks")
    assert "DELETE FROM runbooks_vec WHERE rowid = ?" in _delete_vec_sql("runbooks")


def test_insert_helpers_match_table_schemas():
    assert "INSERT INTO runbooks" in _insert_companion_sql("runbooks")
    assert "(id, source, payload, rowid)" in _insert_companion_sql("runbooks")
    assert "INSERT INTO runbooks_vec" in _insert_vec_sql("runbooks")
    assert "(rowid, embedding)" in _insert_vec_sql("runbooks")


def test_search_sql_joins_vec_and_companion():
    """The JOIN is the whole point — vec0 has rowid + distance,
    companion has id + source + payload."""
    sql = _search_sql("runbooks")
    assert "FROM runbooks_vec AS v" in sql
    assert "JOIN runbooks    AS c ON c.rowid = v.rowid" in sql
    assert "WHERE v.embedding MATCH ? AND v.k = ?" in sql
    assert "ORDER BY v.distance" in sql


def test_search_sql_with_filter_appends_clauses():
    sql = _search_sql("runbooks", "json_extract(c.payload, ?) = ?")
    assert "AND json_extract(c.payload, ?) = ?" in sql


def test_delete_by_source_three_step_pipeline():
    """1. SELECT rowids by source; 2. DELETE from vec; 3. DELETE from companion."""
    assert "SELECT rowid FROM runbooks WHERE source = ?" in _delete_by_source_select_sql("runbooks")
    assert "DELETE FROM runbooks WHERE source = ?" in _delete_by_source_companion_sql("runbooks")
    sql_vec = _delete_by_source_vec_sql("runbooks", "?,?,?")
    assert "DELETE FROM runbooks_vec WHERE rowid IN (?,?,?)" in sql_vec


def test_has_source_limits_to_one():
    assert "LIMIT 1" in _has_source_sql("runbooks")


def test_list_sources_groups_and_extracts_source_type():
    sql = _list_sources_sql("runbooks")
    assert "GROUP BY source" in sql
    assert "json_extract(payload, '$.source_type')" in sql
    assert "COUNT(*)" in sql


# ─── Lazy-conn error paths ────────────────────────────────────────────


def test_get_conn_raises_clearly_when_dep_missing():
    """If the rag-sqlite-vec dep group isn't installed, the error
    message points the operator at the right install command."""
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        # Dep not installed → the error fires from _require_sqlite_vec.
        store = SqliteVecStore(path=":memory:")
        with pytest.raises(RuntimeError, match="rag-sqlite-vec"):
            store._get_conn()
        return
    pytest.skip("rag-sqlite-vec is installed; can't test missing-dep branch here")


# ─── End-to-end roundtrip (gated on dep) ──────────────────────────────


@pytest.fixture
def _sqlite_vec_available():
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        pytest.skip("rag-sqlite-vec optional dep group not installed")


@pytest.mark.asyncio
async def test_roundtrip_initialize_upsert_search(_sqlite_vec_available):
    """Full pipeline against an in-memory SQLite. Proves the SQL shapes
    actually work against sqlite-vec — not just look right."""
    store = SqliteVecStore(path=":memory:")
    await store.initialize("test_coll", dim=4)

    # Upsert three vectors. Vector 1 is closest to the query vector.
    await store.upsert(
        ids=["a", "b", "c"],
        dense=[
            [1.0, 0.0, 0.0, 0.0],   # closest to query
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
        ],
        sparse=None,
        payloads=[
            {"source": "doc1", "text": "alpha"},
            {"source": "doc2", "text": "beta"},
            {"source": "doc3", "text": "gamma"},
        ],
    )

    # Query with vector closest to "a".
    hits = await store.search(dense=[1.0, 0.0, 0.0, 0.0], top_k=3)
    assert len(hits) == 3
    # Closest first — should be "doc1"/"alpha".
    assert hits[0]["source"] == "doc1"
    assert hits[0]["text"] == "alpha"
    assert hits[0]["score"] > hits[1]["score"]

    await store.close()


@pytest.mark.asyncio
async def test_roundtrip_delete_by_source(_sqlite_vec_available):
    store = SqliteVecStore(path=":memory:")
    await store.initialize("test_del", dim=2)
    await store.upsert(
        ids=["x", "y", "z"],
        dense=[[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]],
        sparse=None,
        payloads=[
            {"source": "src-a"},
            {"source": "src-b"},
            {"source": "src-a"},
        ],
    )
    assert await store.has_source("src-a") is True
    assert await store.has_source("src-z") is False

    count = await store.delete_by_source("src-a")
    assert count == 2

    assert await store.has_source("src-a") is False
    assert await store.has_source("src-b") is True
    await store.close()


@pytest.mark.asyncio
async def test_upsert_is_idempotent(_sqlite_vec_available):
    """Same id upserted twice should update, not duplicate. The
    rowid lookup in upsert handles this."""
    store = SqliteVecStore(path=":memory:")
    await store.initialize("test_upsert", dim=2)
    await store.upsert(
        ids=["a"], dense=[[1.0, 0.0]],
        sparse=None, payloads=[{"source": "v1", "version": 1}],
    )
    await store.upsert(
        ids=["a"], dense=[[1.0, 0.0]],
        sparse=None, payloads=[{"source": "v2", "version": 2}],
    )
    sources = await store.list_sources(limit=10)
    # Exactly one row with the updated source.
    assert len(sources) == 1
    assert sources[0]["source"] == "v2"
    assert sources[0]["chunk_count"] == 1
    await store.close()


@pytest.mark.asyncio
async def test_methods_do_not_block_event_loop(tmp_path, monkeypatch):
    """#86: the sqlite-vec hot path (upsert/search/list/has/delete) runs its
    sqlite via asyncio.to_thread — no event-loop blocking. initialize() opens
    the connection outside the guard (first-use startup)."""
    pytest.importorskip("sqlite_vec")          # rag-sqlite-vec extra
    bb = pytest.importorskip("blockbuster")
    monkeypatch.setenv("OBS_SQLITE_VEC_DB", str(tmp_path / "vec.db"))
    store = SqliteVecStore()
    await store.initialize("guard_coll", 4)    # opens conn (unguarded)
    with bb.blockbuster_ctx(["cairn"]):
        await store.upsert(
            ["a"], [[0.1, 0.2, 0.3, 0.4]], None, [{"source": "s", "source_type": "doc"}]
        )
        hits = await store.search([0.1, 0.2, 0.3, 0.4], top_k=3)
        assert await store.has_source("s") is True
        srcs = await store.list_sources()
        await store.delete_by_source("s")
    assert hits and srcs
    await store.close()
