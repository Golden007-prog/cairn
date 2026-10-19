"""Postgres backends for the registry-family stores (#236, epic #224).

The ``workspace_id`` data model is Postgres-ready, but before #236 only the
run/event store had a Postgres backend — the **workspace store** and the
**workflow registry** were sqlite-only and ignored ``OBS_STORAGE``, so a
multi-instance fleet wouldn't share workspaces or installed-workflows
(contradicting ADR-0048's "DB authoritative for the fleet").

Structural tests (always run, no live PG):
  1. The PG stores instantiate lazily (no asyncpg import on construction).
  2. The lazy pool surfaces a clear error when no DSN is supplied.
  3. ``OBS_STORAGE`` routes ``get_workspace_store`` / ``get_workflow_registry``
     to the right impl for every scheme (postgres / postgresql / sqlite / unset).

Live roundtrips (gated on ``PGSTORE_DSN`` + the ``storage-postgres`` dep):
  4. Workspace put/get/list.
  5. Workflow registry: schema/template content-addressing + workspace-scoped
     installed records with supersede semantics.
  6. The run-store ``workspace_id`` JSONB filter against a real Postgres
     (the path that was valid-but-untested-live).
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core import workspace as ws
from cairn.core import workflow_registry as wr
from cairn.core.workspace import (
    PostgresWorkspaceStore,
    SqliteWorkspaceStore,
    Workspace,
)
from cairn.core.workflow_registry import (
    InstalledWorkflow,
    PostgresWorkflowRegistry,
    SqliteWorkflowRegistry,
    WorkflowSchema,
    WorkflowTemplate,
)


# ─── Lazy instantiation + DSN guard ───────────────────────────────────

def test_pg_stores_instantiate_without_dep():
    """Constructors don't import asyncpg — the dep stays lazy/optional."""
    w = PostgresWorkspaceStore(dsn="postgres://nowhere/db")
    r = PostgresWorkflowRegistry(dsn="postgres://nowhere/db")
    assert w._pool is None and w._schema_ready is False
    assert r._pool is None and r._schema_ready is False


@pytest.mark.asyncio
@pytest.mark.parametrize("store_cls", [PostgresWorkspaceStore, PostgresWorkflowRegistry])
async def test_pg_pool_raises_clearly_without_dsn(store_cls):
    store = store_cls()
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="storage-postgres"):
            await store._get_pool()
    else:
        with pytest.raises(RuntimeError, match="CAIRN_STORAGE=postgres"):
            await store._get_pool()


# ─── OBS_STORAGE dispatch (the #236 core: factories honor the URL) ─────

@pytest.mark.parametrize(
    "url,expect_workspace,expect_registry",
    [
        ("postgres://u:p@h/db", PostgresWorkspaceStore, PostgresWorkflowRegistry),
        ("postgresql://u:p@h/db", PostgresWorkspaceStore, PostgresWorkflowRegistry),
        ("sqlite://:memory:", SqliteWorkspaceStore, SqliteWorkflowRegistry),
        ("", SqliteWorkspaceStore, SqliteWorkflowRegistry),
    ],
)
def test_factories_honor_obs_storage(monkeypatch, tmp_path, url, expect_workspace, expect_registry):
    monkeypatch.setenv("OBS_STORAGE", url)
    monkeypatch.setenv("OBS_WORKSPACE_DB", str(tmp_path / "ws.db"))
    monkeypatch.setenv("OBS_WORKFLOW_REGISTRY_DB", str(tmp_path / "wf.db"))
    monkeypatch.setattr(ws, "_store", None)   # force a fresh dispatch
    monkeypatch.setattr(wr, "_store", None)
    try:
        assert isinstance(ws.get_workspace_store(), expect_workspace)
        assert isinstance(wr.get_workflow_registry(), expect_registry)
    finally:
        # construction is lazy (no connection opened) so close() is a no-op,
        # but reset the module singletons so the PG instance can't leak.
        monkeypatch.setattr(ws, "_store", None)
        monkeypatch.setattr(wr, "_store", None)


# ─── Live roundtrips (gated on PGSTORE_DSN + dep) ──────────────────────

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
async def test_live_workspace_store_roundtrip(_pg_dsn):
    store = PostgresWorkspaceStore(dsn=_pg_dsn)
    try:
        # default is seeded on schema-ready
        assert (await store.get("default")) is not None
        await store.put(Workspace(id="pgws-acme", name="Acme"))
        got = await store.get("pgws-acme")
        assert got is not None and got.name == "Acme"
        ids = {w.id for w in await store.list()}
        assert {"default", "pgws-acme"} <= ids
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_live_workflow_registry_scopes_and_supersedes(_pg_dsn):
    import uuid
    suffix = uuid.uuid4().hex[:8]  # unique per run: a reused database keeps old installs (#2551)
    ws_a, ws_b = f"pgws-a-{suffix}", f"pgws-b-{suffix}"
    store = PostgresWorkflowRegistry(dsn=_pg_dsn)
    try:
        # content-addressed schema + template (dedup by hash)
        await store.put_schema(WorkflowSchema(schema_hash="sch_pg1", input_schema={"a": 1}))
        await store.put_template(WorkflowTemplate(template_hash="tpl_pg1", ast={"n": 1}))
        assert (await store.get_schema("sch_pg1")).schema_hash == "sch_pg1"
        assert (await store.get_template("tpl_pg1")).ast == {"n": 1}

        # same ref@version in two workspaces is distinct (no collision)
        await store.record_installed(InstalledWorkflow(workflow_ref="wf/x", workspace_id=ws_a, schema_hash="sch_pg1", version="1"))
        await store.record_installed(InstalledWorkflow(workflow_ref="wf/x", workspace_id=ws_b, schema_hash="sch_pg1", version="1"))
        a = await store.list_installed(ws_a)
        b = await store.list_installed(ws_b)
        assert len(a) == 1 and len(b) == 1
        assert a[0].workspace_id == ws_a and b[0].workspace_id == ws_b

        # a new version in workspace a supersedes the prior active there only
        await store.record_installed(InstalledWorkflow(workflow_ref="wf/x", workspace_id=ws_a, schema_hash="sch_pg2", version="2"))
        active_a = await store.active_for("wf/x", ws_a)
        assert active_a is not None and active_a.version == "2"
        # workspace b is untouched by a's supersede
        active_b = await store.active_for("wf/x", ws_b)
        assert active_b is not None and active_b.version == "1"
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_live_run_store_workspace_filter(_pg_dsn):
    """The JSONB ``data->>'workspace_id'`` filter against a real Postgres —
    valid but previously untested live (#236)."""
    from cairn.core.storage.postgres.runs import PostgresRunStore
    from cairn.core.storage import Run
    from cairn.core.engine.schema import TriggerEvent

    store = PostgresRunStore(dsn=_pg_dsn)
    try:
        def _run(rid, wsid):
            return Run(
                id=rid, trigger=TriggerEvent(source="manual"),
                lane="realtime", fingerprint=rid, status="queued", workspace_id=wsid,
            )
        await store.create(_run("run-ws-a1", "pgws-a"))
        await store.create(_run("run-ws-b1", "pgws-b"))
        a_ids = {r.id for r in await store.list(workspace_id="pgws-a", limit=50)}
        b_ids = {r.id for r in await store.list(workspace_id="pgws-b", limit=50)}
        assert "run-ws-a1" in a_ids and "run-ws-a1" not in b_ids
        assert "run-ws-b1" in b_ids and "run-ws-b1" not in a_ids
    finally:
        await store.close()
