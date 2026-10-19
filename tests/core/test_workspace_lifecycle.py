"""#258 workspace lifecycle — new-workspace-empty, per-workspace enable, and
soft-delete (archive) that never touches runs/events/audit.

Unit tests for enable_workflow + archive/restore; TestClient tests for the
endpoint behaviors (only `default` auto-syncs; archive preserves runs + hides the
workspace; `default` can't be archived).
"""
from __future__ import annotations

import sqlite3
import types

import pytest
from fastapi.testclient import TestClient

from cairn.core import workflow_registry as wr
from cairn.core.engine.schema import TriggerEvent
from cairn.core.storage.protocols import Run
from cairn.core.storage.sqlite.runs import SqliteRunStore
from cairn.core.triggers.server import build_app


# ── unit: enable + archive/restore ──────────────────────────────────────────

async def test_enable_workflow_records_into_the_workspace(monkeypatch):
    """A single discovered workflow is recorded into the target workspace (the
    seams are stubbed — we test enable's wiring, not the whole engine)."""
    from cairn.core import ui_contract as _contract
    from cairn.core.engine import skill_engine as _se
    from cairn.core.engine import compiler as _compiler
    from cairn.core.engine import compiled as _compiled

    integ = types.SimpleNamespace(capability="train.run", operator="train/qlora")
    monkeypatch.setattr(_contract, "build_ui_contract", lambda ref, **kw: types.SimpleNamespace(
        plan={"template_found": True}, inputs={"type": "object"}, outputs={}, integrations=[integ], pack="demo"))
    monkeypatch.setattr(_se, "resolve_template_path", lambda ref, **kw: "/tmp/x.md")
    monkeypatch.setattr(_compiler, "compile_template", lambda p, **kw: types.SimpleNamespace(content_hash="tpl_x", spec=object()))
    monkeypatch.setattr(_compiled, "ast_dict", lambda spec: {"nodes": []})

    store = wr.InMemoryWorkflowRegistry()
    assert await wr.enable_workflow("demo/wf", workspace_id="acme", store=store) is True
    rec = await store.active_for("demo/wf", "acme")
    assert rec is not None and rec.workspace_id == "acme" and rec.required_capabilities == ["train.run"]
    # enabled in acme only — not in another workspace
    assert await store.active_for("demo/wf", "beta") is None
    # an unfound template returns False, records nothing
    monkeypatch.setattr(_contract, "build_ui_contract", lambda ref, **kw: types.SimpleNamespace(plan={"template_found": False}))
    assert await wr.enable_workflow("demo/missing", workspace_id="acme", store=store) is False


async def test_archive_then_restore_flips_record_status():
    store = wr.InMemoryWorkflowRegistry()
    await wr.record_provenance(workflow_ref="p/a", input_schema={}, output_schema={}, workspace_id="acme", store=store)
    await wr.record_provenance(workflow_ref="p/b", input_schema={}, output_schema={}, workspace_id="acme", store=store)
    await wr.record_provenance(workflow_ref="p/a", input_schema={}, output_schema={}, workspace_id="default", store=store)

    n = await wr.archive_workspace("acme", store=store)
    assert n == 2  # both acme records flipped active → archived
    assert all(r.status == "archived" for r in await store.list_installed("acme"))
    assert all(r.status == "active" for r in await store.list_installed("default"))  # untouched

    n = await wr.restore_workspace("acme", store=store)
    assert n == 2 and all(r.status == "active" for r in await store.list_installed("acme"))


async def test_disable_workflow_soft_disables_then_reenables(monkeypatch):
    """disable_workflow flips one workflow's active record to `disabled` (the FE
    lists only active) without deleting it, scoped to the workspace, and is
    reversible by re-recording an active provenance (#273)."""
    store = wr.InMemoryWorkflowRegistry()
    await wr.record_provenance(workflow_ref="p/a", input_schema={}, output_schema={}, workspace_id="acme", store=store)
    await wr.record_provenance(workflow_ref="p/a", input_schema={}, output_schema={}, workspace_id="beta", store=store)

    # disable in acme only: record flips active → disabled, drops out of active_for
    assert await wr.disable_workflow("p/a", workspace_id="acme", store=store) is True
    assert await store.active_for("p/a", "acme") is None
    recs = await store.list_installed("acme")
    assert len(recs) == 1 and recs[0].status == "disabled"  # soft — not deleted
    assert await store.active_for("p/a", "beta") is not None  # other workspace untouched

    # disabling again (nothing active here) → False
    assert await wr.disable_workflow("p/a", workspace_id="acme", store=store) is False
    # re-enable (record a fresh active provenance) restores it
    await wr.record_provenance(workflow_ref="p/a", input_schema={}, output_schema={}, workspace_id="acme", store=store)
    assert await store.active_for("p/a", "acme") is not None


# ── integration: endpoints ──────────────────────────────────────────────────

class _FakeRunner:
    async def run(self, *a, **k):  # pragma: no cover
        return types.SimpleNamespace()


def _run(rid: str, ws: str) -> Run:
    return Run(id=rid, fingerprint=rid, lane="realtime", status="done",
               trigger=TriggerEvent(source="pattern", matched_pattern="x", service="svc"),
               workspace_id=ws, created_at="2026-01-01T00:00:00Z")


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)
    monkeypatch.setenv("OBS_STORAGE", f"sqlite:///{tmp_path / 'runs.db'}")
    monkeypatch.setenv("OBS_WORKSPACE_DB", str(tmp_path / "ws.db"))
    # inject a fresh, empty workflow registry so the test isn't perturbed by the
    # process-cached store (a prior test may have populated `default`).
    reg = wr.InMemoryWorkflowRegistry()
    monkeypatch.setattr(wr, "get_workflow_registry", lambda: reg)
    return TestClient(build_app(_FakeRunner(), enable_scheduler=False))


def test_only_default_auto_syncs_on_empty(tmp_path, monkeypatch):
    """A non-default workspace starts EMPTY (no discovery auto-sync); `default`
    auto-syncs. We stub the sync so the test is hermetic + fast."""
    calls: list[str] = []
    async def _fake_sync(*, workspace_id=None, store=None):
        calls.append(workspace_id); return 0
    monkeypatch.setattr(wr, "sync_discovered_workflows", _fake_sync)
    client = _client(tmp_path, monkeypatch)

    assert client.get("/workspaces/acme/registry/installed-workflows").json() == {"workflows": []}
    assert calls == []  # a new workspace did NOT trigger a sync
    client.get("/workspaces/default/registry/installed-workflows")
    assert calls == ["default"]  # only default auto-syncs


def test_archive_hides_workspace_but_preserves_runs(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    # a workspace with a run
    client.post("/workspaces", json={"name": "acme"})
    SqliteRunStore(str(tmp_path / "runs.db")).close()
    conn = sqlite3.connect(str(tmp_path / "runs.db"))
    r = _run("r1", "acme")
    conn.execute("INSERT INTO runs (id, fingerprint, lane, status, workspace_id, created_at, data) VALUES (?,?,?,?,?,?,?)",
                 (r.id, r.fingerprint, r.lane, r.status, r.workspace_id, r.created_at, r.model_dump_json()))
    conn.commit(); conn.close()
    assert [i["id"] for i in client.get("/workspaces/acme/runs").json()["investigations"]] == ["r1"]

    # archive → workspace hidden from the switcher list, but the run is untouched.
    # backend-enforced typed delete (#323): the archive needs ?confirm=delete.
    assert client.delete("/workspaces/acme?confirm=delete").status_code == 200
    assert "acme" not in [w["id"] for w in client.get("/workspaces").json()["workspaces"]]
    assert [i["id"] for i in client.get("/workspaces/acme/runs").json()["investigations"]] == ["r1"]  # preserved!

    # restore brings it back
    assert client.patch("/workspaces/acme", json={"status": "active"}).status_code == 200
    assert "acme" in [w["id"] for w in client.get("/workspaces").json()["workspaces"]]


def test_default_cannot_be_archived(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    # pass the typed-delete confirm (#323) so this asserts the *default-guard*
    # 400, not the missing-confirm 400.
    r = client.delete("/workspaces/default?confirm=delete")
    assert r.status_code == 400 and "default" in r.json()["detail"]


def test_ui_triggers_scoped_to_enabled_workflows(tmp_path, monkeypatch,
                                                 vendor_triggers_dir):
    """GLOBAL triggers surface in a workspace only when the workflow they fire is
    enabled there (#271). `default` auto-syncs discovery so it shows them; a fresh
    workspace inherits NONE until the workflow is enabled — no tenancy leak.

    #1543 — "global" is layer ① (pack) OR layer ② (overlay); `_visible` in
    console_triggers.py scopes both by the same rule. The incident pack used to
    supply the layer-① rows here, but its triggers/ was deleted (live ingress
    around the rca pipeline's gate), which made this test self-skip and silently
    lose the tenancy assertion. An overlay dir restores it.
    """
    pytest.importorskip("cairn_platform")  # /ui/api/triggers is the platform's webui provider (#2427)
    monkeypatch.setenv("OBS_TRIGGERS_DIR", str(vendor_triggers_dir))
    client = _client(tmp_path, monkeypatch)
    pack = [t for t in client.get("/workspaces/default/ui/api/triggers").json()["triggers"]
            if t["origin"] in ("pack", "overlay") and t["template"]]
    if not pack:
        pytest.skip("engine ships no global triggers to scope")
    tmpl = pack[0]["template"]

    # a fresh workspace inherits none of default's pack triggers
    assert client.get("/workspaces/beta/ui/api/triggers").json()["triggers"] == []

    # enabling the workflow there surfaces its trigger (and only then)
    assert client.post("/workspaces/beta/registry/enable", json={"ref": tmpl}).status_code == 200
    after = [t["template"] for t in client.get("/workspaces/beta/ui/api/triggers").json()["triggers"]]
    assert tmpl in after


def test_enable_then_disable_endpoints(tmp_path, monkeypatch):
    """POST /registry/enable then /registry/disable round-trip through the path-scoped
    routes (#273): enable makes a workflow active in the workspace, disable soft-hides
    it (drops from the active listing), a second disable 404s."""
    # stub enable's heavy contract/compile path — record straight into the store.
    async def _fake_enable(ref, *, workspace_id=None, store=None):
        await wr.record_provenance(workflow_ref=ref, input_schema={}, output_schema={},
                                   workspace_id=workspace_id, store=store or wr.get_workflow_registry())
        return True
    monkeypatch.setattr(wr, "enable_workflow", _fake_enable)
    client = _client(tmp_path, monkeypatch)

    assert client.post("/workspaces/acme/registry/enable", json={"ref": "p/a"}).status_code == 200
    active = [w["workflow_ref"] for w in client.get("/workspaces/acme/registry/installed-workflows").json()["workflows"] if w["status"] == "active"]
    assert active == ["p/a"]

    assert client.post("/workspaces/acme/registry/disable", json={"ref": "p/a"}).status_code == 200
    active = [w["workflow_ref"] for w in client.get("/workspaces/acme/registry/installed-workflows").json()["workflows"] if w["status"] == "active"]
    assert active == []  # soft-hidden — the record persists with status `disabled`

    # nothing active to disable now → 404
    assert client.post("/workspaces/acme/registry/disable", json={"ref": "p/a"}).status_code == 404
