"""ADR-0051 §3/§4 — `workspace_id` is a real INDEXED column (not a JSON-blob
`json_extract`) across the workspace-scoped stores, scoping is query-enforced, and
pre-existing (blob-only) DBs migrate on connect (ADR-0050 primitive).

Covers runs (sqlite + in-memory), triggers (sqlite), remote jobs (sqlite).
"""
from __future__ import annotations

import sqlite3

from cairn.core.engine.schema import TriggerEvent
from cairn.core.operator.job_store import RemoteJobRecord, SqliteRemoteJobStore
from cairn.core.storage.memory.runs import InMemoryRunStore
from cairn.core.storage.protocols import Run
from cairn.core.storage.sqlite.runs import SqliteRunStore
from cairn.core.storage import migrations
from cairn.core.storage.trigger_store import SqliteTriggerStore, StoredTrigger


def _run(rid: str, ws: str) -> Run:
    return Run(id=rid, fingerprint=rid, lane="realtime", status="done",
               trigger=TriggerEvent(source="pattern", matched_pattern="x", service="svc"),
               workspace_id=ws, created_at="2026-01-01T00:00:00Z")


def _indexes(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)).fetchall()]


# ── runs ──────────────────────────────────────────────────────────────────

async def test_runs_workspace_id_is_indexed_column_and_scoped(tmp_path):
    store = SqliteRunStore(str(tmp_path / "runs.db"))
    try:
        await store.create(_run("r1", "acme"))
        await store.create(_run("r2", "default"))
        # the dimension is a real column with a covering index, not json_extract
        assert "workspace_id" in migrations.columns(store._conn, "runs")
        assert "idx_run_ws_created" in _indexes(store._conn, "runs")
        # list is workspace-scoped
        assert [r.id for r in await store.list(workspace_id="acme")] == ["r1"]
        assert [r.id for r in await store.list(workspace_id="default")] == ["r2"]
        # get is query-enforced: cross-workspace id is not-found
        assert (await store.get("r1", "acme")) is not None
        assert (await store.get("r1", "default")) is None
        assert (await store.get("r1")) is not None  # unscoped still works
    finally:
        store.close()


async def test_runs_migrate_pre_workspace_id_column(tmp_path):
    """A runs.db created before the workspace_id column (workspace only in the
    JSON blob) gains the column + back-fills from the blob on connect."""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, fingerprint TEXT, lane TEXT, "
                 "status TEXT, created_at TEXT, data TEXT NOT NULL)")
    r = _run("r1", "acme")
    conn.execute("INSERT INTO runs (id, fingerprint, lane, status, created_at, data) VALUES (?,?,?,?,?,?)",
                 (r.id, r.fingerprint, r.lane, r.status, r.created_at, r.model_dump_json()))
    conn.commit()
    conn.close()

    store = SqliteRunStore(path)
    try:
        assert "workspace_id" in migrations.columns(store._conn, "runs")
        # the pre-existing row's blob workspace_id was back-filled into the column
        assert store._conn.execute("SELECT workspace_id FROM runs WHERE id='r1'").fetchone()[0] == "acme"
        assert [r.id for r in await store.list(workspace_id="acme")] == ["r1"]
        assert (await store.get("r1", "default")) is None
    finally:
        store.close()


async def test_inmemory_runs_get_is_workspace_enforced():
    store = InMemoryRunStore()
    await store.create(_run("r1", "acme"))
    assert (await store.get("r1", "acme")) is not None
    assert (await store.get("r1", "default")) is None
    assert (await store.get("r1")) is not None


# ── triggers ────────────────────────────────────────────────────────────────

def _trig(name: str, ws: str) -> StoredTrigger:
    return StoredTrigger(name=name, yaml="source: s\n", source="s", workspace_id=ws)


async def test_triggers_workspace_id_indexed_and_scoped(tmp_path):
    store = SqliteTriggerStore(str(tmp_path / "trig.db"))
    a = _trig("a", "acme")
    b = _trig("b", "default")
    await store.create(a)
    await store.create(b)
    assert "workspace_id" in migrations.columns(store._conn, "triggers")
    assert "idx_trig_ws_name" in _indexes(store._conn, "triggers")
    assert [t.name for t in await store.list("acme")] == ["a"]
    assert (await store.get(a.id, "acme")) is not None
    assert (await store.get(a.id, "default")) is None  # query-enforced


async def test_triggers_migrate_pre_workspace_id_column(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE triggers (id TEXT PRIMARY KEY, name TEXT UNIQUE, source TEXT, data TEXT NOT NULL)")
    t = _trig("a", "acme")
    conn.execute("INSERT INTO triggers (id, name, source, data) VALUES (?,?,?,?)",
                 (t.id, t.name, t.source, t.model_dump_json()))
    conn.commit()
    conn.close()
    store = SqliteTriggerStore(path)
    assert "workspace_id" in migrations.columns(store._conn, "triggers")
    assert store._conn.execute("SELECT workspace_id FROM triggers WHERE name='a'").fetchone()[0] == "acme"
    assert [t.name for t in await store.list("acme")] == ["a"]


# ── remote jobs ──────────────────────────────────────────────────────────────

def _job(jid: str, ws: str) -> RemoteJobRecord:
    return RemoteJobRecord(id=jid, run_id=jid, workspace_id=ws)


async def test_remote_jobs_workspace_id_indexed_and_scoped(tmp_path):
    store = SqliteRemoteJobStore(str(tmp_path / "jobs.db"))
    await store.register(_job("j1", "acme"))
    await store.register(_job("j2", "default"))
    assert "workspace_id" in migrations.columns(store._conn, "remote_jobs")
    assert "idx_jobs_ws_created" in _indexes(store._conn, "remote_jobs")
    assert [j.id for j in await store.list(workspace_id="acme")] == ["j1"]
    assert [j.id for j in await store.list(workspace_id="default")] == ["j2"]


async def test_remote_jobs_migrate_pre_workspace_id_column(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE remote_jobs (id TEXT PRIMARY KEY, run_id TEXT, status TEXT, "
                 "created_at TEXT, data TEXT NOT NULL)")
    j = _job("j1", "acme")
    conn.execute("INSERT INTO remote_jobs (id, run_id, status, created_at, data) VALUES (?,?,?,?,?)",
                 (j.id, j.run_id, j.status, j.created_at, j.model_dump_json()))
    conn.commit()
    conn.close()
    store = SqliteRemoteJobStore(path)
    assert "workspace_id" in migrations.columns(store._conn, "remote_jobs")
    assert store._conn.execute("SELECT workspace_id FROM remote_jobs WHERE id='j1'").fetchone()[0] == "acme"
    assert [j.id for j in await store.list(workspace_id="acme")] == ["j1"]
