"""#628 — POST /runs/{id}/resume must fail CLOSED and honor the JSON body.

The old signature took ``decision: str = "approve"`` as a *query* param only, so:
  * omitting the decision silently APPROVED the gate, and
  * a client that POSTed ``{"decision": "reject"}`` had its body ignored and
    fell through to the approve default — turning a human's rejection into an
    approval (the governance footgun).

These tests pin the fix: no decision → 422 (never approve); the JSON body is
read; and the legacy ``?decision=`` query form still works (back-compat). Fully
hermetic — FakeRunner, no LLM, no backend, no Postgres.
"""
from __future__ import annotations

import os
import types

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest
from fastapi.testclient import TestClient

import cairn.core.triggers.server as server_mod
from cairn.core.triggers.server import build_app
from cairn.core.dispatch.dispatcher import RunDispatcher


class FakeRunner:
    def __init__(self):
        self.default_environment = "production"
        self.skill_engine = types.SimpleNamespace(list_skills=lambda: [])

    async def run(self, *a, **k):
        return []


@pytest.fixture
def client_and_capture(monkeypatch):
    """A TestClient whose dispatcher.get passes the ws-guard and whose
    resume_run records the decision dict it was handed (instead of driving a
    real runtime)."""
    captured: dict = {}

    async def fake_get(self, run_id):  # passes _run_in_ws_or_404
        return types.SimpleNamespace(workspace_id="w", status="awaiting_approval")

    async def fake_resume(self, run_id, decision):
        captured["decision"] = decision
        return {"run_id": run_id, "resumed": True}

    monkeypatch.setattr(RunDispatcher, "get", fake_get)
    monkeypatch.setattr(RunDispatcher, "resume_run", fake_resume)
    import cairn.core.triggers.routers.run_control as run_control_mod
    monkeypatch.setattr(run_control_mod, "current_workspace", lambda: "w")
    return TestClient(build_app(FakeRunner(), enable_scheduler=False)), captured


def test_missing_decision_is_422_not_silent_approve(client_and_capture):
    client, captured = client_and_capture
    resp = client.post("/runs/run-1/resume")  # no body, no query
    assert resp.status_code == 422
    assert "decision" in resp.json()["detail"].lower()
    assert "decision" not in captured  # resume_run must NOT have been called


def test_empty_json_body_is_422(client_and_capture):
    client, captured = client_and_capture
    resp = client.post("/runs/run-1/resume", json={})
    assert resp.status_code == 422
    assert "decision" not in captured


def test_json_body_reject_is_honored_not_approved(client_and_capture):
    client, captured = client_and_capture
    resp = client.post("/runs/run-1/resume", json={"decision": "reject", "approver": "alice"})
    assert resp.status_code == 200
    assert captured["decision"]["approved"] is False  # the footgun: must be False
    assert captured["decision"]["approver"] == "alice"


def test_json_body_approve_is_honored(client_and_capture):
    client, captured = client_and_capture
    resp = client.post("/runs/run-1/resume", json={"decision": "approve"})
    assert resp.status_code == 200
    assert captured["decision"]["approved"] is True


def test_legacy_query_reject_still_works(client_and_capture):
    """Back-compat: the CLI/TUI/web all POST ?decision= with no body."""
    client, captured = client_and_capture
    resp = client.post("/runs/run-1/resume?decision=reject&approver=console")
    assert resp.status_code == 200
    assert captured["decision"]["approved"] is False
    assert captured["decision"]["approver"] == "console"


def test_legacy_query_approve_still_works(client_and_capture):
    client, captured = client_and_capture
    resp = client.post("/runs/run-1/resume?decision=approve&approver=web")
    assert resp.status_code == 200
    assert captured["decision"]["approved"] is True
