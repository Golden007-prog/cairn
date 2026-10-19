"""Workspace-scope triggers (#238) + remote-job store (#239), epic #224.

The #223/#236 pass scoped runs/audit/installed-workflows/RAG/memory; these were
the remaining next-tier stores. Light approach (mirrors the run-store): a
``workspace_id`` field + a list/get/delete filter on the JSON blob — no schema
migration that would break existing ``~/.cairn/*.db`` files. Default workspace
keeps prior behavior.

#238: the trigger router stays global (``list_sync(None)`` = all workspaces);
the *console* lists/gets/deletes within its workspace. #239: the console lists
remote jobs within its workspace; the control worker's ``claim_active`` stays
cross-workspace (shared infra).
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.storage.trigger_store import (
    InMemoryTriggerStore,
    SqliteTriggerStore,
    StoredTrigger,
)
from cairn.core.operator.job_store import (
    InMemoryRemoteJobStore,
    RemoteJobRecord,
    SqliteRemoteJobStore,
)


# ── #238 triggers ────────────────────────────────────────────────────

def _trig(name, ws):
    return StoredTrigger(name=name, yaml=f"# {name}", source="github", workspace_id=ws)


def test_stored_trigger_defaults_to_default_workspace():
    assert StoredTrigger(name="t", yaml="x").workspace_id == "default"


@pytest.mark.parametrize("make_store", [
    lambda: InMemoryTriggerStore(),
    lambda: SqliteTriggerStore(path=":memory:"),
])
async def test_trigger_store_scopes_list_get_delete(make_store):
    store = make_store()
    try:
        await store.create(_trig("nightly", "acme"))
        await store.create(_trig("hourly", "beta"))

        # list scoped vs global (the global router build passes no workspace)
        assert {t.name for t in await store.list("acme")} == {"nightly"}
        assert {t.name for t in await store.list("beta")} == {"hourly"}
        assert {t.name for t in await store.list()} == {"nightly", "hourly"}
        assert {t.name for t in store.list_sync(None)} == {"nightly", "hourly"}

        # cross-workspace get/delete by id are refused (isolation)
        assert await store.get("nightly", "beta") is None
        assert await store.get("nightly", "acme") is not None
        assert await store.delete("nightly", "beta") is False
        assert await store.get("nightly", "acme") is not None  # still there
        assert await store.delete("nightly", "acme") is True
        assert await store.get("nightly", "acme") is None
    finally:
        store.close()


# ── #239 remote-job store ────────────────────────────────────────────

def _job(run_id, ws, status="submitted"):
    return RemoteJobRecord(run_id=run_id, node_name="train", provider="runpod",
                           job_id=f"j-{run_id}", backend="remote_job", workspace_id=ws, status=status)


def test_remote_job_defaults_to_default_workspace():
    assert RemoteJobRecord(run_id="r").workspace_id == "default"


@pytest.mark.parametrize("make_store", [
    lambda: InMemoryRemoteJobStore(),
    lambda: SqliteRemoteJobStore(path=":memory:"),
])
async def test_remote_job_store_scopes_list_but_not_claim(make_store):
    store = make_store()
    try:
        await store.register(_job("run-a", "acme"))
        await store.register(_job("run-b", "beta"))

        # console list is workspace-scoped
        assert {r.run_id for r in await store.list(workspace_id="acme")} == {"run-a"}
        assert {r.run_id for r in await store.list(workspace_id="beta")} == {"run-b"}
        assert {r.run_id for r in await store.list()} == {"run-a", "run-b"}

        # the control worker claims across ALL workspaces (shared infra)
        claimed = await store.claim_active()
        assert {r.run_id for r in claimed} == {"run-a", "run-b"}
    finally:
        store.close()
