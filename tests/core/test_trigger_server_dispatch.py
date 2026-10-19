"""Integration tests for the trigger_server → dispatcher wiring.

Verifies that webhooks now route through the RunDispatcher (gaining a
queryable lifecycle record) and that the status endpoints surface it. Uses a
FakeRunner — no LLM, no backend.
"""
from __future__ import annotations

import os
import types

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest
from fastapi.testclient import TestClient

from cairn.core.triggers.server import build_app


@pytest.fixture(autouse=True)
def _isolated_sqlite_store(tmp_path, monkeypatch):
    """#758 — pin the run store to a fresh per-test sqlite file.

    These server-route tests only exercise the dispatcher's run lifecycle; they
    don't need Postgres. Under a local ``OBS_STORAGE=postgres://…`` they shared
    one asyncpg pool with the completing run and flaked with "another operation
    is in progress" (``dispatcher.list()`` racing a connection reset). Forcing
    sqlite here — regardless of the ambient/.env value — removes that coupling
    while keeping each test isolated (a new tmp file per test)."""
    monkeypatch.setenv("OBS_STORAGE", f"sqlite:///{tmp_path / 'runs.db'}")


def _result(skill="db-pool", template="incident-rca", actions=2):
    return types.SimpleNamespace(
        skill_name=skill, template_name=template,
        duration_seconds=0.01, actions=[object()] * actions,
    )


class FakeRunner:
    """Minimal Runner stand-in for the dispatcher + app to drive."""

    def __init__(self):
        self.default_environment = "production"
        self.skill_engine = types.SimpleNamespace(list_skills=lambda: ["db-pool"])
        self.calls = []

    async def run(self, event, *, environment=None, event_sink=None, run_id=None, _cascade_depth=0):
        self.calls.append(event)
        # 'nomatch' service simulates "no skill matched"
        if event.service == "nomatch":
            return []
        return [_result()]


def _client():
    return TestClient(build_app(FakeRunner(), enable_scheduler=False))


def test_pattern_trigger_returns_investigation_handle():
    client = _client()
    resp = client.post("/triggers/pattern", json={
        "matched_pattern": "connection pool exhausted",
        "service": "payment-service",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"].startswith("run-")
    assert body["status"] == "done"
    assert body["lane"] == "realtime"
    assert body["matched_skills"] == 1
    assert body["results"][0]["skill_name"] == "db-pool"
    assert body["results"][0]["actions_proposed"] == 2


def test_no_match_reports_zero_skills():
    client = _client()
    resp = client.post("/triggers/pattern", json={
        "matched_pattern": "whatever", "service": "nomatch",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["matched_skills"] == 0
    assert body["status"] == "done"
    assert body["results"] == []


def test_investigation_is_queryable_after_dispatch():
    client = _client()
    submit = client.post("/triggers/pattern", json={
        "matched_pattern": "connection pool exhausted", "service": "payment-service",
    }).json()
    inv_id = submit["run_id"]

    # list
    listed = client.get("/runs").json()
    assert listed["count"] == 1
    assert listed["investigations"][0]["id"] == inv_id

    # get by id
    got = client.get(f"/runs/{inv_id}").json()
    assert got["id"] == inv_id
    assert got["status"] == "done"
    assert got["lane"] == "realtime"

    # filter by status
    done = client.get("/runs?status=done").json()
    assert done["count"] == 1


def test_get_unknown_investigation_404():
    client = _client()
    assert client.get("/runs/run-does-not-exist").status_code == 404


def test_cancel_terminal_investigation_is_noop():
    client = _client()
    inv_id = client.post("/triggers/manual", json={"skill_name": "db-pool"}).json()["run_id"]
    # already done → cancel returns it unchanged (not an error)
    resp = client.post(f"/runs/{inv_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"


def test_cancel_unknown_investigation_404():
    client = _client()
    assert client.post("/runs/nope/cancel").status_code == 404


# ── #896 HITL approval lifecycle endpoints (the reachable HTTP glue; the positive
#    awaiting_approval → expired path is covered by test_approval_reaper.py) ──


def test_expire_non_awaiting_run_is_409():
    client = _client()
    inv_id = client.post("/triggers/manual", json={"skill_name": "db-pool"}).json()["run_id"]
    # the run is 'done', not awaiting approval → expiring it is a 409 (mirrors the
    # resume terminal-state guard #618), never a silent transition.
    resp = client.post(f"/runs/{inv_id}/expire")
    assert resp.status_code == 409
    assert "not awaiting approval" in resp.json()["detail"]
    assert client.get(f"/runs/{inv_id}").json()["status"] == "done"  # untouched


def test_expire_unknown_run_is_404():
    client = _client()
    assert client.post("/runs/nope/expire").status_code == 404


def test_bulk_dismiss_all_stale_on_empty_queue_is_zero():
    client = _client()
    resp = client.post("/approvals/dismiss", json={"all_stale": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0 and body["expired"] == []


def test_bulk_dismiss_skips_a_resolved_run():
    # fail-closed: naming a done run never expires it (only awaiting_approval runs).
    client = _client()
    inv_id = client.post("/triggers/manual", json={"skill_name": "db-pool"}).json()["run_id"]
    resp = client.post("/approvals/dismiss", json={"run_ids": [inv_id, "ghost"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["expired"] == [] and set(body["skipped"]) == {inv_id, "ghost"}
    assert client.get(f"/runs/{inv_id}").json()["status"] == "done"


def test_mounting_armed_trigger_routes_skips_paths_already_mounted():
    """#2631 — boot mounts the webhook paths the pipeline-trigger pass armed AFTER the
    app was built; a path mounted at build time is not mounted a second time."""
    from fastapi import FastAPI

    from cairn.core.engine.triggers_spec import load_trigger_spec_text
    from cairn.core.triggers.router import TriggerRouter
    from cairn.core.triggers.server import _mount_trigger_routes

    def spec(path):
        return load_trigger_spec_text(
            f"source: custom\ningress: {{transport: http, path: {path}, method: POST}}\nnormalize: {{}}\nrun: {{template: pipeline:p/x}}\n",
            name=path)

    app = FastAPI()
    _mount_trigger_routes(app, TriggerRouter([spec("/triggers/a")]))
    _mount_trigger_routes(app, TriggerRouter([spec("/triggers/a"), spec("/triggers/b")]),
                          skip_paths={r.path for r in app.routes})
    paths = [r.path for r in app.routes if r.path.startswith("/triggers/")]
    assert sorted(paths) == ["/triggers/a", "/triggers/b"]
