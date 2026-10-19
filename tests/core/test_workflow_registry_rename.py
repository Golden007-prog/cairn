"""Tests for the workflow-registry SQLite table rename (epic #427).

The three tables (``schemas``, ``templates``, ``installed``) are renamed to
``workflow_schemas``/``workflow_templates``/``workflow_installed`` so they're
safe to share a single ``~/.cairn/cairn.db`` with other stores — matching the
already-prefixed :class:`~cairn.core.workflow_registry.PostgresWorkflowRegistry`
names. A fresh DB gets the prefixed names directly; a DB carrying the old bare
names is renamed in place on connect, preserving data.
"""
from __future__ import annotations

import sqlite3

from cairn.core import workflow_registry as wr


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


async def test_fresh_sqlite_registry_creates_prefixed_tables(tmp_path):
    """A brand-new DB gets the ``workflow_``-prefixed table names, never the old
    bare ones — so it's immediately safe to share with other stores."""
    path = str(tmp_path / "fresh.db")
    store = wr.SqliteWorkflowRegistry(path)
    try:
        names = _tables(store._conn)
        assert {"workflow_schemas", "workflow_templates", "workflow_installed"} <= names
        assert "schemas" not in names
        assert "templates" not in names
        assert "installed" not in names
    finally:
        store.close()


async def test_old_named_tables_migrated_in_place_preserving_rows(tmp_path):
    """A DB pre-seeded with the ORIGINAL bare-named tables + data is upgraded on
    connect: the old tables are renamed (not dropped/recreated), so seeded rows
    survive under the new names."""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schemas (hash TEXT PRIMARY KEY, data TEXT NOT NULL)")
    conn.execute("CREATE TABLE templates (hash TEXT PRIMARY KEY, data TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE installed (id TEXT, ref TEXT, workspace_id TEXT NOT NULL DEFAULT 'default', "
        "status TEXT, data TEXT NOT NULL, PRIMARY KEY (workspace_id, id))"
    )
    sch = wr.WorkflowSchema(schema_hash="sch_abc", input_schema={"type": "object"})
    tpl = wr.WorkflowTemplate(template_hash="tpl_abc", ast={"nodes": []})
    rec = wr.InstalledWorkflow(id="p/w@1", workflow_ref="p/w", status="active", schema_hash="sch_abc")
    conn.execute("INSERT INTO schemas (hash, data) VALUES (?,?)", (sch.schema_hash, sch.model_dump_json()))
    conn.execute("INSERT INTO templates (hash, data) VALUES (?,?)", (tpl.template_hash, tpl.model_dump_json()))
    conn.execute(
        "INSERT INTO installed (id, ref, workspace_id, status, data) VALUES (?,?,?,?,?)",
        (rec.id, rec.workflow_ref, "default", rec.status, rec.model_dump_json()),
    )
    conn.commit()
    conn.close()

    store = wr.SqliteWorkflowRegistry(path)
    try:
        names = _tables(store._conn)
        # old bare names are gone (renamed, not left dangling alongside the new ones)
        assert "schemas" not in names
        assert "templates" not in names
        assert "installed" not in names
        assert {"workflow_schemas", "workflow_templates", "workflow_installed"} <= names

        # the seeded rows survive under the new table names, reachable via the API
        got_schema = await store.get_schema("sch_abc")
        assert got_schema is not None and got_schema.input_schema == {"type": "object"}

        got_template = await store.get_template("tpl_abc")
        assert got_template is not None and got_template.ast == {"nodes": []}

        got_installed = await store.get_installed("p/w@1", "default")
        assert got_installed is not None and got_installed.workflow_ref == "p/w"
        assert await store.active_for("p/w", "default") is not None
    finally:
        store.close()


async def test_migration_is_idempotent_on_second_connect(tmp_path):
    """Reopening an already-migrated DB is a no-op rename-wise (guarded by
    presence of the new table) and doesn't lose data."""
    path = str(tmp_path / "twice.db")
    store1 = wr.SqliteWorkflowRegistry(path)
    h = await wr.record_provenance(
        workflow_ref="p/w", input_schema={"properties": {"a": {}}}, output_schema={}, store=store1,
    )
    store1.close()

    store2 = wr.SqliteWorkflowRegistry(path)
    try:
        names = _tables(store2._conn)
        assert {"workflow_schemas", "workflow_templates", "workflow_installed"} <= names
        assert (await store2.get_schema(h)) is not None
        assert (await store2.active_for("p/w")) is not None
    finally:
        store2.close()
