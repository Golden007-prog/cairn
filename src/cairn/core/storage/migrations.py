"""Shared SQLite schema-migration helpers — the first slice of the central
schema home (ADR-0050).

Stores in this codebase are raw ``sqlite3`` (+ asyncpg for Postgres) with no ORM.
Each created its schema with ``CREATE TABLE IF NOT EXISTS`` in its ``__init__`` —
which silently does **nothing** when the table already exists with an *older*
shape. So a column / primary-key change shipped after the first release never
reached already-created DBs, and the next query referencing the new column raised
``sqlite3.OperationalError: no such column`` (#255 — the ``workspace_id`` column
added to ``workflow_registry.installed`` by the workspace work).

These helpers give every store one tested way to inspect an existing table and
bring it up to the current schema on connect. SQLite can ``ALTER TABLE … ADD
COLUMN`` but **cannot** change a primary key, so a PK change requires the
create-copy-drop-rename rebuild in :func:`rebuild_table`.

ADR-0050 builds on this: a central per-store schema definition + a migration
runner. This module is the reusable primitive that runner stands on; stores adopt
it incrementally.
"""
from __future__ import annotations

import sqlite3


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """True if ``table`` exists in the connected DB."""
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Column names of ``table`` (empty if it doesn't exist)."""
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def pk_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Primary-key column names in key order. ``PRAGMA table_info`` returns the
    1-based key position in the ``pk`` field (0 = not part of the PK)."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    pk = sorted((r for r in rows if r[5]), key=lambda r: r[5])  # r[5] = pk position
    return [r[1] for r in pk]


def add_missing_columns(
    conn: sqlite3.Connection, table: str, defs: dict[str, str]
) -> list[str]:
    """``ALTER TABLE … ADD COLUMN`` for each name in ``defs`` absent from ``table``.

    ``defs`` maps a column name → its full declaration (type + constraints +
    ``DEFAULT``; a non-constant default is rejected by SQLite on ADD COLUMN, so
    use a literal). Returns the names actually added. Idempotent — safe to call on
    every connect. Use this when only columns were added; for a PK change use
    :func:`rebuild_table`.
    """
    have = set(columns(conn, table))
    added: list[str] = []
    for name, decl in defs.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            added.append(name)
    return added


def rebuild_table(
    conn: sqlite3.Connection,
    table: str,
    *,
    new_schema: str,
    select_columns: str,
    from_select: str,
) -> None:
    """Rebuild ``table`` to a new schema — the only way to change a PRIMARY KEY in
    SQLite (``ALTER TABLE`` can't touch the PK).

    Creates a ``{table}__new`` with column spec ``new_schema``, copies rows via
    ``INSERT INTO {table}__new ({select_columns}) {from_select}`` (where
    ``from_select`` is a ``SELECT … FROM {table}`` that back-fills any columns the
    old table lacks with literals), then drops the old table and renames the new
    one into place. The caller is responsible for the surrounding transaction /
    ``commit``; a stale ``{table}__new`` from an aborted prior run is dropped first.
    """
    tmp = f"{table}__new"
    conn.execute(f"DROP TABLE IF EXISTS {tmp}")
    conn.execute(f"CREATE TABLE {tmp} ({new_schema})")
    conn.execute(f"INSERT INTO {tmp} ({select_columns}) {from_select}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
