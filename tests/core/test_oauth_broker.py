"""Outbound OAuth broker seam (#100, #310).

Covers the replaceable seam (registry + default + env selection — the shipped
default is AuthlibBroker, #310) and the ``resolve_connection`` ``kind=oauth``
branch that hands a fresh access token to downstream operators identically to
an api_key connection. AuthlibBroker's own dance (start/complete/refresh/revoke)
is covered in test_oauth_authlib.py. All hermetic.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.connections import (
    Connection,
    InMemoryConnectionStore,
    resolve_connection,
)
from cairn.core.oauth import (
    AuthlibBroker,
    get_oauth_broker,
    register_broker,
)


# ── registry / replaceable seam ────────────────────────────────────────


def test_default_broker_is_authlib(monkeypatch):
    # #310 — the built-in in-process broker is the shipped default.
    monkeypatch.delenv("OBS_OAUTH_BROKER", raising=False)
    b = get_oauth_broker()
    assert isinstance(b, AuthlibBroker)
    assert b.name == "authlib"


def test_unknown_broker_raises():
    with pytest.raises(KeyError):
        get_oauth_broker("does-not-exist")


def test_env_selects_broker(monkeypatch):
    class _Fake:
        name = "fake-sel"

    register_broker("fake-sel", _Fake)
    monkeypatch.setenv("OBS_OAUTH_BROKER", "fake-sel")
    assert isinstance(get_oauth_broker(), _Fake)        # global default honored
    assert isinstance(get_oauth_broker("authlib"), AuthlibBroker)  # explicit overrides


# ── resolve_connection kind=oauth branch ───────────────────────────────


@pytest.mark.asyncio
async def test_resolve_connection_oauth_delegates_to_broker():
    class _FakeBroker:
        name = "fake-resolve"

        async def get_access_token(self, *, provider, connection_id):
            assert provider == "github"
            assert connection_id == "conn-xyz"     # config["connection_id"] used
            return "TOKEN-FROM-BROKER"

        async def start_authorization(self, **_):  # pragma: no cover - unused here
            ...

        async def revoke(self, **_):  # pragma: no cover - unused here
            ...

    register_broker("fake-resolve", _FakeBroker)
    store = InMemoryConnectionStore()
    await store.create(Connection(
        name="gh",
        kind="oauth",
        config={"broker": "fake-resolve", "provider": "github",
                "connection_id": "conn-xyz"},
        secrets={"BROKER_KEY": "NANGO_SECRET_KEY"},   # broker key is a ref, not the token
    ))

    resolved = await resolve_connection("gh", store=store)
    assert resolved is not None
    assert resolved.kind == "oauth"
    assert resolved.secrets == {"ACCESS_TOKEN": "TOKEN-FROM-BROKER"}  # operator-facing
    assert "TOKEN-FROM-BROKER" not in repr(resolved)                 # masked in repr


@pytest.mark.asyncio
async def test_resolve_connection_oauth_falls_back_to_name_as_connection_id():
    captured = {}

    class _FakeBroker:
        name = "fake-fallback"

        async def get_access_token(self, *, provider, connection_id):
            captured["connection_id"] = connection_id
            return "T"

        async def start_authorization(self, **_):  # pragma: no cover
            ...

        async def revoke(self, **_):  # pragma: no cover
            ...

    register_broker("fake-fallback", _FakeBroker)
    store = InMemoryConnectionStore()
    await store.create(Connection(
        name="slack-conn", kind="oauth",
        config={"broker": "fake-fallback", "provider": "slack"},  # no connection_id
    ))
    await resolve_connection("slack-conn", store=store)
    assert captured["connection_id"] == "slack-conn"   # falls back to the name


# ── the redirect target is D2 (#2427) ──────────────────────────────────

def test_public_server_serves_the_oauth_callback(monkeypatch):
    """``cairn connect --via oauth`` redirects to ``/oauth/callback`` by default, so the public
    server mounts it with no ``cairn.routers`` provider installed (ADR-0123 D1, #2427)."""
    from fastapi.testclient import TestClient

    from cairn.core.oauth import broker as B
    from cairn.core.triggers import router_providers
    from cairn.core.triggers.server import build_app
    from tests.core.test_trigger_server_dispatch import FakeRunner

    async def _complete(self, *, state, code):
        assert (state, code) == ("s1", "c1")
        return "conn-1"

    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)
    monkeypatch.setattr(router_providers, "load_router_providers", lambda: [])
    monkeypatch.setattr(B.AuthlibBroker, "complete_authorization", _complete)
    c = TestClient(build_app(FakeRunner(), enable_scheduler=False))
    assert c.get("/oauth/callback").status_code == 400
    assert "OAuth error: denied" in c.get("/oauth/callback", params={"error": "denied"}).text
    r = c.get("/oauth/callback", params={"state": "s1", "code": "c1"})
    assert r.status_code == 200 and "Connected: conn-1" in r.text
