"""Two companies, one deployment (#1613 + #1610, ADR-0094).

The reason #1610 was sequenced BEFORE signup. Until it landed, a workspace was
self-asserted (#1609): the middleware took whatever the caller named and ran the
request inside it. Signup is what turns that from a filing quirk into one
company reading another's audit trail.

This is the end-to-end acceptance for the pair — a real app, auth ON, two
accounts created through the real signup route.
"""
from __future__ import annotations

import os
import types

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest


@pytest.fixture()
def two_tenants(tmp_path, monkeypatch):
    """A real app with auth on, `open` signup, and two companies registered."""
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret-long-enough-for-hs256-abc")
    monkeypatch.setenv("OBS_SIGNUP", "open")
    monkeypatch.setenv("OBS_ACCOUNT_DB", str(tmp_path / "a.db"))
    monkeypatch.setenv("OBS_RUN_DB", str(tmp_path / "r.db"))
    monkeypatch.setenv("OBS_WORKSPACE_DB", str(tmp_path / "w.db"))
    monkeypatch.delenv("OBS_API_USER", raising=False)
    monkeypatch.delenv("OBS_API_PASSWORD", raising=False)

    # Setting the *_DB env vars is not enough: both stores are process-wide
    # SINGLETONS, so whichever test module ran first already built them and the
    # env var is read no more. Patch the singletons themselves, or this file
    # passes alone and fails in the suite — which is exactly what it did.
    from cairn.core import account as _acct
    from cairn.core import workspace as _ws
    monkeypatch.setattr(_acct, "_store", _acct.SqliteAccountStore(path=str(tmp_path / "a.db")))
    monkeypatch.setattr(_ws, "_store", _ws.SqliteWorkspaceStore(path=str(tmp_path / "w.db")))

    from fastapi.testclient import TestClient
    from cairn.core.triggers.server import build_app

    class FakeRunner:
        default_environment = "production"
        skill_engine = types.SimpleNamespace(list_skills=lambda: [])
        async def run(self, *a, **k):
            return {}

    c = TestClient(build_app(FakeRunner(), enable_scheduler=False))

    def register(company: str, email: str) -> dict:
        c.post("/auth/signup", json={"company": company, "email": email, "password": "pw12345"})
        tok = c.post("/token", data={"username": email, "password": "pw12345"}).json()["access_token"]
        return {"Authorization": f"Bearer {tok}"}

    return c, register("Acme Inc", "a@acme.com"), register("Globex", "b@globex.com")


def test_neither_company_sees_the_others_workspaces(two_tenants):
    c, acme, globex = two_tenants
    a = {w["id"] for w in c.get("/workspaces", headers=acme).json()["workspaces"]}
    b = {w["id"] for w in c.get("/workspaces", headers=globex).json()["workspaces"]}
    assert a and b, "each company got a workspace"
    assert a.isdisjoint(b), "workspace lists must not overlap across accounts"


def test_asserting_another_accounts_workspace_by_header_is_404(two_tenants):
    """THE attack the audit found open (#1609): one header, another tenant."""
    c, _, globex = two_tenants
    r = c.get("/runs", headers={**globex, "X-Cairn-Workspace": "acme-inc"})
    assert r.status_code == 404


def test_asserting_another_accounts_workspace_by_path_is_404(two_tenants):
    """The middleware accepts BOTH spellings, so the weaker one must not become
    the way in."""
    c, _, globex = two_tenants
    assert c.get("/workspaces/acme-inc/runs", headers=globex).status_code == 404


def test_it_is_404_and_not_403(two_tenants):
    """Cross-tenant EXISTENCE must not leak — "you may not" and "there is no
    such thing" have to be indistinguishable (ADR-0051 §4)."""
    c, _, globex = two_tenants
    assert c.get("/workspaces/acme-inc/runs", headers=globex).status_code != 403


def test_a_company_can_still_use_its_own_workspace(two_tenants):
    """The boundary has to refuse the other tenant WITHOUT breaking the owner."""
    c, acme, _ = two_tenants
    assert c.get("/workspaces/acme-inc/runs", headers=acme).status_code == 200


def test_the_founder_owns_their_account(two_tenants):
    c, acme, _ = two_tenants
    me = c.get("/auth/me", headers=acme).json()
    assert me["account_id"] == "acme-inc"
    assert me["role"] == "owner" and me["is_admin"] is True


def test_an_unstamped_workspace_is_not_visible_to_every_account(two_tenants):
    """A tenancy filter must fail CLOSED.

    `GET /workspaces` compared `(w.account_id or acct) == acct`, which reads as
    "a workspace with no account stamp belongs to whoever is asking" — so a
    single unstamped row appeared in EVERY company's workspace switcher. The
    fallback is now DEFAULT_ACCOUNT, so an unstamped workspace belongs to the
    implicit default account and nobody else.

    Reachable through a hand-written or migrated row rather than either creation
    path, which is exactly why the filter has to be the thing that refuses.
    """
    c, acme, globex = two_tenants
    import asyncio

    from cairn.core.workspace import Workspace, get_workspace_store
    asyncio.run(get_workspace_store().put(Workspace(id="orphan-ws", name="Orphan", account_id="")))

    seen_a = {w["id"] for w in c.get("/workspaces", headers=acme).json()["workspaces"]}
    seen_b = {w["id"] for w in c.get("/workspaces", headers=globex).json()["workspaces"]}
    assert "orphan-ws" not in seen_a, "an unstamped workspace leaked into acme's switcher"
    assert "orphan-ws" not in seen_b, "an unstamped workspace leaked into globex's switcher"
