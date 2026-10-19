"""#618 — control-plane restart mid-run must not falsely cancel/resume durable runs.

Live on Temporal (2026-07-09) a `docker restart` of the server cancelled a paused
run, and `POST /resume` on that cancelled run returned 200 + signalled Temporal —
orphaning the workflow. Two guards pinned here:

  * Bug A — `_preserve_on_shutdown`: a shutdown-time CancelledError under a durable
    runtime must NOT mark the run cancelled (the workflow survives in Temporal/DBOS).
  * Bug B — resume rejects a non-`awaiting_approval` run with 409 (fail-closed).

Hermetic — FakeRunner, sqlite, no LLM/Postgres/Temporal.
"""
from __future__ import annotations

import os
import types

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest
from fastapi.testclient import TestClient

import cairn.core.storage as storage_mod
import cairn.core.dispatch.dispatcher as disp_mod
import cairn.core.triggers.server as server_mod
from cairn.core.dispatch.dispatcher import RunDispatcher
from cairn.core.storage import build_storage
from cairn.core.triggers.server import build_app
from tests.core.test_trigger_server_dispatch import FakeRunner


# ── Bug A — _preserve_on_shutdown ────────────────────────────────────────
@pytest.fixture
def dispatcher(tmp_path, monkeypatch):
    monkeypatch.setenv("OBS_STORAGE", f"sqlite://{tmp_path}/r.db")
    monkeypatch.setattr(storage_mod, "_STORAGE", None)
    return RunDispatcher(FakeRunner(), backend=build_storage())


def test_preserve_false_when_not_shutting_down(dispatcher, monkeypatch):
    monkeypatch.setattr(disp_mod._settings, "runtime", lambda: "temporal")
    assert dispatcher._shutting_down is False
    assert dispatcher._preserve_on_shutdown("r1") is False  # normal cancel → still cancels


def test_preserve_true_on_shutdown_durable(dispatcher, monkeypatch):
    dispatcher._shutting_down = True
    for rt in ("temporal", "dbos"):
        monkeypatch.setattr(disp_mod._settings, "runtime", lambda rt=rt: rt)
        assert dispatcher._preserve_on_shutdown("r1") is True  # durable workflow survives → don't cancel


def test_preserve_false_on_shutdown_in_process(dispatcher, monkeypatch):
    """in_process has no durable workflow — a shutdown genuinely loses the run,
    so marking it cancelled is correct (must NOT preserve)."""
    dispatcher._shutting_down = True
    monkeypatch.setattr(disp_mod._settings, "runtime", lambda: "in_process")
    assert dispatcher._preserve_on_shutdown("r1") is False


async def test_aclose_sets_shutting_down(dispatcher):
    assert dispatcher._shutting_down is False
    await dispatcher.aclose()
    assert dispatcher._shutting_down is True


# ── Bug B — resume rejects non-awaiting_approval runs (409) ───────────────
@pytest.fixture
def client_for_status(monkeypatch):
    """Build a TestClient whose dispatcher.get returns a run with a chosen status."""
    def _make(status: str):
        async def fake_get(self, run_id):
            return types.SimpleNamespace(workspace_id="w", status=status)

        async def fake_resume(self, run_id, decision):
            return {"run_id": run_id, "resumed": True}

        monkeypatch.setattr(RunDispatcher, "get", fake_get)
        monkeypatch.setattr(RunDispatcher, "resume_run", fake_resume)
        import cairn.core.triggers.routers.run_control as run_control_mod
        monkeypatch.setattr(run_control_mod, "current_workspace", lambda: "w")
        return TestClient(build_app(FakeRunner(), enable_scheduler=False))
    return _make


@pytest.mark.parametrize("status", ["cancelled", "done", "failed", "running", "queued"])
def test_resume_rejects_non_awaiting_approval(client_for_status, status):
    client = client_for_status(status)
    resp = client.post("/runs/run-x/resume", json={"decision": "approve"})
    assert resp.status_code == 409, f"{status} should 409, got {resp.status_code}"
    assert status in resp.json()["detail"]


def test_resume_allows_awaiting_approval(client_for_status):
    client = client_for_status("awaiting_approval")
    resp = client.post("/runs/run-x/resume", json={"decision": "approve"})
    assert resp.status_code == 200
    assert resp.json()["resumed"] is True
