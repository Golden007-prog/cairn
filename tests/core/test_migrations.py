"""Unit tests for the shared SQLite migration primitives (ADR-0050,
``cairn.core.storage.migrations``) — the reusable mechanism behind the #255 fix.
"""
from __future__ import annotations

import sqlite3

import pytest

from cairn.core.storage import migrations


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


def test_table_exists_and_columns(conn):
    assert migrations.table_exists(conn, "t") is False
    conn.execute("CREATE TABLE t (a TEXT, b INTEGER)")
    assert migrations.table_exists(conn, "t") is True
    assert migrations.columns(conn, "t") == ["a", "b"]
    assert migrations.columns(conn, "missing") == []


def test_pk_columns_single_and_composite(conn):
    conn.execute("CREATE TABLE one (id TEXT PRIMARY KEY, x TEXT)")
    conn.execute("CREATE TABLE two (ws TEXT, id TEXT, x TEXT, PRIMARY KEY (ws, id))")
    assert migrations.pk_columns(conn, "one") == ["id"]
    assert migrations.pk_columns(conn, "two") == ["ws", "id"]  # key order preserved


def test_add_missing_columns_is_idempotent(conn):
    conn.execute("CREATE TABLE t (a TEXT)")
    added = migrations.add_missing_columns(conn, "t", {
        "a": "TEXT",  # already present → skipped
        "ws": "TEXT NOT NULL DEFAULT 'default'",
        "n": "INTEGER",
    })
    assert added == ["ws", "n"]
    assert migrations.columns(conn, "t") == ["a", "ws", "n"]
    # second call adds nothing
    assert migrations.add_missing_columns(conn, "t", {"ws": "TEXT", "n": "INTEGER"}) == []


def test_rebuild_table_changes_pk_and_backfills(conn):
    # old shape: single-column PK, no `ws`
    conn.execute("CREATE TABLE t (id TEXT PRIMARY KEY, val TEXT)")
    conn.execute("INSERT INTO t (id, val) VALUES ('a', '1'), ('b', '2')")
    migrations.rebuild_table(
        conn, "t",
        new_schema="ws TEXT NOT NULL DEFAULT 'default', id TEXT, val TEXT, PRIMARY KEY (ws, id)",
        select_columns="ws, id, val",
        from_select="SELECT 'default', id, val FROM t",
    )
    assert migrations.pk_columns(conn, "t") == ["ws", "id"]
    rows = sorted(conn.execute("SELECT ws, id, val FROM t").fetchall())
    assert rows == [("default", "a", "1"), ("default", "b", "2")]
    # the composite PK now lets the same id live under two workspaces
    conn.execute("INSERT INTO t (ws, id, val) VALUES ('acme', 'a', '9')")
    assert conn.execute("SELECT count(*) FROM t WHERE id='a'").fetchone()[0] == 2
