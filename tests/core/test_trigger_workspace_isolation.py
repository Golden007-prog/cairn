"""A webhook receiver belongs to one workspace, queued or inline.

Two ways a receiver's runs could land in a workspace that did not declare it:

* **The durable inbox (#212).** With ``CAIRN_TRIGGER_DURABLE`` on, a matched event is
  queued and a drain loop starts it later. The queued record stored no workspace,
  so the drain started every run in the process default, wherever the receiver was
  declared. The record now carries the receiver's workspace and the drain binds the
  run to it (a replayed record keeps it too).
* **Two workspaces, one path (#213).** Receivers are keyed by ingress path, so two
  workspaces declaring a receiver at the same path collapsed to one, the last row
  loaded, and one workspace's sender started runs in the other's. Such a path is
  now refused at load with an error naming the workspaces: no receiver is mounted
  there until one of them moves.

Driven through the real app (``build_app``) with auth on.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import types

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest
from fastapi.testclient import TestClient

from cairn.core.engine.runner import Runner
from cairn.core.storage import trigger_inbox as inbox_mod
from cairn.core.storage.trigger_inbox import SqliteTriggerInbox, drain_once
from cairn.core.storage.trigger_store import StoredTrigger
from cairn.core.triggers import auth
from cairn.core.triggers import router as router_mod
from cairn.core.triggers.router import TriggerRouter
from cairn.core.triggers.server import build_app
from cairn.core.workspace import current_workspace

SECRET = "hook-secret"
BODY = json.dumps({"type": "push"}).encode()


def _spec_text(path: str) -> str:
    return (
        "source: custom\n"
        f"ingress: {{transport: http, path: '{path}', method: POST, signature: "
        "{header: X-Signature, secret: '${env:HOOK_SECRET}', required: true}}\n"
        "normalize: {event_type: type}\n"
        "run: {template: pipeline:p/x}\n"
    )


def _row(rid: str, ws: str, path: str) -> StoredTrigger:
    return StoredTrigger(id=rid, workspace_id=ws, name=rid, source="custom",
                         yaml=_spec_text(path), enabled=True)


def _sig(secret: str = SECRET, body: bytes = BODY) -> dict[str, str]:
    return {"X-Signature": hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
            "content-type": "application/json"}


class _Started:
    """The launch-door stub (the inline path): records each start and its workspace."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, ref, inputs, *, environment=None):
        self.calls.append((ref, current_workspace()))
        return type("Run", (), {"id": f"run-{len(self.calls)}", "status": "queued"})()


@pytest.fixture
def started() -> _Started:
    return _Started()


@pytest.fixture
def stored(monkeypatch):
    """The trigger store the router loads receivers from, and no overlay files."""
    rows: list[StoredTrigger] = []
    store = types.SimpleNamespace(list_sync=lambda *a, **k: list(rows), close=lambda: None)
    import cairn.core.storage.trigger_store as ts

    monkeypatch.setattr(ts, "default_trigger_store", lambda: store)
    monkeypatch.setattr(router_mod, "load_trigger_specs", lambda *_a, **_k: [])
    return rows


@pytest.fixture
def env(monkeypatch, started):
    for k in ("OBS_JWT_ALGORITHM", "OBS_ACCESS_TOKEN_EXPIRE_MINUTES", "OBS_REQUIRE_AUTH",
              "OBS_WORKSPACE", "CAIRN_WORKSPACE", "OBS_TRIGGER_DURABLE", "CAIRN_TRIGGER_DURABLE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret-long-enough-for-hs256-abc")
    monkeypatch.setenv("HOOK_SECRET", SECRET)
    monkeypatch.setattr(auth, "_PUBLIC_TRIGGER_PATHS", set())  # module state: start clean
    import cairn.core.dispatch.start as start_mod

    # the ONE launch door build_app hands the router; the stub records where it ran
    monkeypatch.setattr(start_mod, "unattended_starter", lambda *a, **k: started)


# ═══ #212 — a queued delivery keeps its receiver's workspace ═══════════════


@pytest.fixture
def durable(monkeypatch, env):
    """Durable ingress on an in-memory inbox. The lifespan's drain loop is captured
    (inbox + the server's own dispatch handler) so the test drains it once, and the
    in-process start the drain makes is recorded with the workspace it ran in."""
    monkeypatch.setenv("OBS_TRIGGER_DURABLE", "1")
    monkeypatch.setenv("OBS_TRIGGER_INBOX", "memory")
    captured: dict = {}

    async def _capture(inbox, handler, **_kw):
        captured["inbox"], captured["handler"] = inbox, handler

    monkeypatch.setattr(inbox_mod, "run_drain_loop", _capture)
    ran: list[tuple[str, dict, str]] = []

    async def _start(ref, inputs):
        ran.append((ref, dict(inputs), current_workspace()))
        return {}

    monkeypatch.setattr(router_mod, "start_template_run", _start)

    async def _no_sync(**_kw):
        return 0

    import cairn.core.workflow_registry as wr

    monkeypatch.setattr(wr, "sync_discovered_pipelines", _no_sync)
    return captured, ran


def _drain(captured) -> dict:
    return asyncio.run(drain_once(captured["inbox"], captured["handler"]))


def test_a_queued_run_lands_in_its_receivers_workspace(durable, stored, started):
    captured, ran = durable
    stored.append(_row("acme-hook", "acme-ops", "/triggers/acme"))
    with TestClient(build_app(Runner(), enable_scheduler=False)) as c:
        r = c.post("/triggers/acme", content=BODY, headers=_sig())
        assert r.status_code == 200, (r.status_code, r.text)
        assert _drain(captured)["dispatched"] == 1
    assert [(ref, ws) for ref, _i, ws in ran] == [("pipeline:p/x", "acme-ops")]
    assert started.calls == []  # queued, not started inline


def test_a_replayed_record_keeps_its_workspace(durable, stored):
    captured, ran = durable
    stored.append(_row("acme-hook", "acme-ops", "/triggers/acme"))
    with TestClient(build_app(Runner(), enable_scheduler=False)) as c:
        assert c.post("/triggers/acme", content=BODY, headers=_sig()).status_code == 200
        _drain(captured)
        tok = auth.create_access_token({"sub": "admin"})
        r = c.post("/triggers/inbox/replay", params={"since": "1970-01-01T00:00:00"},
                   headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200, (r.status_code, r.text)
        assert _drain(captured)["dispatched"] == 1
    assert [ws for *_x, ws in ran] == ["acme-ops", "acme-ops"]


def test_a_queued_run_for_a_default_workspace_receiver_still_replays_there(durable, stored):
    captured, ran = durable
    stored.append(_row("dflt-hook", "default", "/triggers/dflt"))
    with TestClient(build_app(Runner(), enable_scheduler=False)) as c:
        assert c.post("/triggers/dflt", content=BODY, headers=_sig()).status_code == 200
        assert _drain(captured)["dispatched"] == 1
    assert [ws for *_x, ws in ran] == ["default"]


def test_a_record_queued_before_the_field_existed_replays_in_the_default(tmp_path):
    """A row written by an older server has no ``workspace``: it reads back as None,
    which binds nothing, so it runs where every queued run ran before."""
    db = str(tmp_path / "inbox.db")
    inbox = SqliteTriggerInbox(db)
    rec = asyncio.run(inbox.enqueue("custom", "pipeline:p/x", {}, workspace="acme-ops"))
    old = rec.model_dump(mode="json")
    old.pop("workspace")
    inbox._conn.execute("UPDATE inbox SET data=? WHERE id=?", (json.dumps(old), rec.id))
    inbox._conn.commit()
    (row,) = asyncio.run(inbox.list())
    assert row.workspace is None
    rec2 = asyncio.run(inbox.enqueue("custom", "pipeline:p/x", {}, workspace="acme-ops"))
    assert [r.workspace for r in asyncio.run(inbox.list()) if r.id == rec2.id] == ["acme-ops"]
    inbox.close()


# ═══ #213 — one ingress path, one workspace ════════════════════════════════


def test_two_workspaces_at_one_path_are_refused_at_load(env, stored, started, caplog):
    stored.extend([_row("a-hook", "acme-ops", "/triggers/shared"),
                   _row("g-hook", "globex", "/triggers/shared")])
    with caplog.at_level(logging.ERROR):
        c = TestClient(build_app(Runner(), enable_scheduler=False))
    assert not auth._is_public_path("/triggers/shared", "POST")
    r = c.post("/triggers/shared", content=BODY, headers=_sig())
    assert r.status_code == 401, (r.status_code, r.text)
    tok = auth.create_access_token({"sub": "admin"})
    r = c.post("/triggers/shared", content=BODY,
               headers={**_sig(), "Authorization": f"Bearer {tok}"})
    assert r.status_code in (404, 405), (r.status_code, r.text)  # no receiver mounted
    assert started.calls == []
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("/triggers/shared" in m and "acme-ops" in m and "globex" in m for m in msgs), msgs


def test_the_router_has_no_receiver_at_a_contested_path(env, stored):
    stored.extend([_row("a-hook", "acme-ops", "/triggers/shared"),
                   _row("g-hook", "globex", "/triggers/shared"),
                   _row("a-own", "acme-ops", "/triggers/acme")])
    r = TriggerRouter.from_installed()
    assert r.spec_for_path("/triggers/shared") is None
    assert "/triggers/shared" not in r.paths()
    assert r.workspace_for(r.spec_for_path("/triggers/acme")) == "acme-ops"


def test_one_workspaces_receiver_still_works(env, stored, started):
    stored.extend([_row("a-hook", "acme-ops", "/triggers/shared"),
                   _row("g-hook", "globex", "/triggers/shared"),
                   _row("a-own", "acme-ops", "/triggers/acme")])
    c = TestClient(build_app(Runner(), enable_scheduler=False))
    r = c.post("/triggers/acme", content=BODY, headers=_sig())
    assert r.status_code == 200, (r.status_code, r.text)
    assert started.calls == [("pipeline:p/x", "acme-ops")]


def test_the_same_workspace_declaring_a_path_twice_is_unchanged(env, stored, started):
    """Not a cross-workspace collision: the last row wins, as before."""
    stored.extend([_row("a-1", "acme-ops", "/triggers/acme"),
                   _row("a-2", "acme-ops", "/triggers/acme")])
    c = TestClient(build_app(Runner(), enable_scheduler=False))
    r = c.post("/triggers/acme", content=BODY, headers=_sig())
    assert r.status_code == 200, (r.status_code, r.text)
    assert started.calls == [("pipeline:p/x", "acme-ops")]
