"""Workspace variables (ADR-0101 D2, #1974) — the store round-trip and the routes.

Two halves:

* the **store** across the two hermetic backends (in-memory + sqlite `:memory:`),
  pinning that a variable is scoped to its workspace and that a falsy value
  survives a round-trip (`0`, `false` and `""` are legitimate values here, and a
  store that swallowed them would silently disable a cost cap);
* the **routes**, over `build_app`, addressed the way the console addresses them
  — `/workspaces/{id}/variables` through the ADR-0051 path rewrite, NOT a flat
  `/variables` call. That is the part worth a server test: the handlers are
  declared flat because `_WorkspaceMiddleware` rewrites the path, so a route
  declared at the literal `/workspaces/{id}/…` spelling would be dead code and
  a test that only called `/variables` would never notice.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest
from fastapi.testclient import TestClient

from cairn.core.engine.runner import Runner
from cairn.core.triggers.server import build_app
from cairn.core.workspace_variables import (
    RESERVED_KEYS,
    InMemoryWorkspaceVariableStore,
    SqliteWorkspaceVariableStore,
    WorkspaceVariable,
    valid_key,
    variables_map,
)


def _stores():
    return [InMemoryWorkspaceVariableStore(), SqliteWorkspaceVariableStore(":memory:")]


# ── the store ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("store", _stores(), ids=["memory", "sqlite"])
async def test_variable_round_trip(store):
    await store.put(WorkspaceVariable(workspace_id="acme", key="environment", value="prod",
                                      description="where runs land", updated_by="ops-lead"))
    await store.put(WorkspaceVariable(workspace_id="acme", key="cost_cap_usd", value=5))

    rows = await store.list("acme")
    assert [v.key for v in rows] == ["cost_cap_usd", "environment"]          # sorted by key
    env = next(v for v in rows if v.key == "environment")
    assert (env.value, env.description, env.updated_by) == ("prod", "where runs land", "ops-lead")
    assert env.updated_at                                                    # stamped on write

    one = await store.get("acme", "cost_cap_usd")
    assert one is not None and one.value == 5
    assert await store.get("acme", "nope") is None

    # a put on an existing key REPLACES it (idempotent, one row per key)
    await store.put(WorkspaceVariable(workspace_id="acme", key="environment", value="staging"))
    assert (await store.get("acme", "environment")).value == "staging"
    assert len(await store.list("acme")) == 2

    assert await store.delete("acme", "environment") is True
    assert await store.delete("acme", "environment") is False                # already gone
    assert [v.key for v in await store.list("acme")] == ["cost_cap_usd"]
    store.close()


@pytest.mark.parametrize("store", _stores(), ids=["memory", "sqlite"])
async def test_variables_are_scoped_to_their_workspace(store):
    await store.put(WorkspaceVariable(workspace_id="acme", key="environment", value="prod"))
    await store.put(WorkspaceVariable(workspace_id="other", key="environment", value="dev"))

    assert await variables_map("acme", store=store) == {"environment": "prod"}
    assert await variables_map("other", store=store) == {"environment": "dev"}
    # deleting one workspace's key leaves the other's alone
    await store.delete("acme", "environment")
    assert await variables_map("other", store=store) == {"environment": "dev"}
    store.close()


# A FACTORY, not an instance: `_stores()` is evaluated once at collection, so a
# shared instance would already be closed by the time the second `value` case ran.
@pytest.mark.parametrize(
    "make_store",
    [InMemoryWorkspaceVariableStore, lambda: SqliteWorkspaceVariableStore(":memory:")],
    ids=["memory", "sqlite"],
)
@pytest.mark.parametrize("value", [0, False, "", None, {"a": [1, 2]}, 1.5])
async def test_falsy_and_structured_values_survive(make_store, value):
    """`cost_cap_usd: 0` must not come back as a missing cap."""
    store = make_store()
    await store.put(WorkspaceVariable(workspace_id="w", key="k", value=value))
    got = await store.get("w", "k")
    assert got is not None and got.value == value
    store.close()


def test_valid_key_rejects_what_would_break_the_primary_key():
    assert valid_key("environment") and valid_key("my-var_1.x")
    assert not valid_key("")
    assert not valid_key("   ")
    assert not valid_key("has:colon")   # ':' separates (workspace, key) in the row id
    assert not valid_key("has/slash")   # would swallow the next path segment
    assert not valid_key("x" * 129)


def test_reserved_keys_are_the_four_the_run_dialog_reads():
    assert set(RESERVED_KEYS) == {"environment", "config_preset", "cost_cap_usd", "series"}


# ── the routes ───────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)         # keyless dev → auth open
    monkeypatch.setenv("OBS_STORAGE", "")
    monkeypatch.setenv("OBS_WORKSPACE_VARS_DB", str(tmp_path / "vars.db"))
    monkeypatch.setenv("OBS_WORKSPACE_DB", str(tmp_path / "ws.db"))
    import cairn.core.workspace_variables as wv
    monkeypatch.setattr(wv, "_default_store", None)             # per-test singleton
    yield TestClient(build_app(Runner(), enable_scheduler=False, enable_spec_routes=False))
    monkeypatch.setattr(wv, "_default_store", None)


def test_put_list_delete_through_the_workspace_path(client):
    empty = client.get("/workspaces/acme/variables")
    assert empty.status_code == 200, empty.text
    assert empty.json()["variables"] == []
    assert empty.json()["reserved_keys"] == list(RESERVED_KEYS)

    put = client.put("/workspaces/acme/variables/environment",
                     json={"value": "prod", "description": "where runs land"})
    assert put.status_code == 200, put.text
    assert put.json()["key"] == "environment" and put.json()["value"] == "prod"
    assert put.json()["workspace_id"] == "acme"

    listed = client.get("/workspaces/acme/variables").json()
    assert [(v["key"], v["value"]) for v in listed["variables"]] == [("environment", "prod")]
    assert listed["workspace_id"] == "acme"

    # a second put replaces rather than duplicates
    client.put("/workspaces/acme/variables/environment", json={"value": "staging"})
    assert [v["value"] for v in client.get("/workspaces/acme/variables").json()["variables"]] == ["staging"]

    assert client.delete("/workspaces/acme/variables/environment").status_code == 200
    assert client.get("/workspaces/acme/variables").json()["variables"] == []


def test_one_workspace_does_not_see_anothers_variables(client):
    client.put("/workspaces/acme/variables/environment", json={"value": "prod"})
    client.put("/workspaces/other/variables/environment", json={"value": "dev"})
    assert [v["value"] for v in client.get("/workspaces/acme/variables").json()["variables"]] == ["prod"]
    assert [v["value"] for v in client.get("/workspaces/other/variables").json()["variables"]] == ["dev"]


def test_a_bad_key_is_refused_and_a_missing_one_is_a_404(client):
    bad = client.put("/workspaces/acme/variables/has:colon", json={"value": 1})
    assert bad.status_code == 422, bad.text
    assert client.delete("/workspaces/acme/variables/never-set").status_code == 404


def test_value_is_required_so_an_omitted_one_cannot_null_the_key(client):
    client.put("/workspaces/acme/variables/cost_cap_usd", json={"value": 5})
    assert client.put("/workspaces/acme/variables/cost_cap_usd", json={"description": "oops"}).status_code == 422
    assert client.get("/workspaces/acme/variables").json()["variables"][0]["value"] == 5
