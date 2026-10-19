"""Built-in AuthlibBroker + OAuth catalog + token store (#116).

Hermetic: the provider token endpoint is a httpx MockTransport, so the
authorization-code exchange + refresh are exercised with no real provider.
"""
from __future__ import annotations

import importlib.util
import os
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import httpx
import pytest

from cairn.core.oauth.catalog import get_oauth_provider, list_oauth_providers, load_oauth_catalog
from cairn.core.oauth.store import OAuthTokenStore

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("authlib") is None, reason="needs the [oauth] extra (authlib)"
)


# ── catalog (scopes live in the file) ────────────────────────────────────


def test_catalog_loads_providers_and_scopes():
    cat = load_oauth_catalog()
    assert {"github", "slack", "google", "notion", "jira"} <= set(cat)
    gh = get_oauth_provider("github")
    assert gh.scopes == ["repo", "read:org"]
    assert gh.authorize_url.startswith("https://github.com/login/oauth/authorize")
    g = get_oauth_provider("google")
    assert g.refresh is True and g.pkce is True and g.extra_authorize_params.get("access_type") == "offline"
    assert any(p.provider == "jira" for p in list_oauth_providers())


# ── writable token store ─────────────────────────────────────────────────


async def test_token_store_round_trips():
    s = OAuthTokenStore(":memory:")
    await s.put_pending("st8", {"provider": "github", "connection_id": "gh1"})
    assert (await s.pop_pending("st8"))["connection_id"] == "gh1"
    assert await s.pop_pending("st8") is None  # consumed
    await s.put_token("gh1", "github", {"access_token": "AT", "refresh_token": "RT"})
    assert (await s.get_token("gh1"))["access_token"] == "AT"
    assert await s.delete_token("gh1") is True
    assert await s.get_token("gh1") is None
    s.close()


# ── AuthlibBroker (mock token endpoint) ──────────────────────────────────


def _broker(handler, monkeypatch):
    from cairn.core.oauth.broker import AuthlibBroker

    monkeypatch.setenv("OBS_OAUTH_GITHUB_CLIENT_ID", "cid-test")
    monkeypatch.setenv("OBS_OAUTH_GITHUB_CLIENT_SECRET", "sec-test")
    return AuthlibBroker(store=OAuthTokenStore(":memory:"), transport=httpx.MockTransport(handler))


def test_broker_registered_under_authlib():
    from cairn.core.oauth import AuthlibBroker, get_oauth_broker

    assert isinstance(get_oauth_broker("authlib"), AuthlibBroker)


async def test_start_authorization_builds_url_and_stores_pending(monkeypatch):
    broker = _broker(lambda r: httpx.Response(500), monkeypatch)  # no token call here
    session = await broker.start_authorization(provider="github", connection_id="gh1")
    q = parse_qs(urlparse(session.redirect_url).query)
    assert session.redirect_url.startswith("https://github.com/login/oauth/authorize")
    assert q["client_id"] == ["cid-test"] and q["state"] and q["scope"] == ["repo read:org"]
    assert "redirect_uri" in q
    # pending was persisted under the state
    assert (await broker._tokens().pop_pending(q["state"][0]))["connection_id"] == "gh1"


async def test_complete_then_get_access_token(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"access_token": "AT-1", "refresh_token": "RT-1", "token_type": "bearer", "expires_in": 3600})

    broker = _broker(handler, monkeypatch)
    session = await broker.start_authorization(provider="github", connection_id="gh1")
    state = parse_qs(urlparse(session.redirect_url).query)["state"][0]
    cid = await broker.complete_authorization(state=state, code="the-code")
    assert cid == "gh1"
    assert await broker.get_access_token(provider="github", connection_id="gh1") == "AT-1"


async def test_get_access_token_refreshes_when_expired(monkeypatch):
    import time

    def handler(request):
        return httpx.Response(200, json={"access_token": "AT-2", "token_type": "bearer", "expires_in": 3600})

    broker = _broker(handler, monkeypatch)
    # seed an expired token with a refresh token
    await broker._tokens().put_token(
        "gh1", "github", {"access_token": "AT-old", "refresh_token": "RT-1", "expires_at": time.time() - 100}
    )
    assert await broker.get_access_token(provider="github", connection_id="gh1") == "AT-2"


async def test_get_access_token_without_token_errors(monkeypatch):
    from cairn.core.oauth import OAuthBrokerError

    broker = _broker(lambda r: httpx.Response(500), monkeypatch)
    with pytest.raises(OAuthBrokerError, match="no token"):
        await broker.get_access_token(provider="github", connection_id="nope")


async def test_revoke_deletes_token(monkeypatch):
    broker = _broker(lambda r: httpx.Response(500), monkeypatch)
    await broker._tokens().put_token("gh1", "github", {"access_token": "AT"})
    await broker.revoke(provider="github", connection_id="gh1")
    with_token = await broker._tokens().get_token("gh1")
    assert with_token is None


async def test_complete_authorization_creates_usable_connection(monkeypatch, tmp_path):
    """#310 — completing the dance upserts a kind=oauth Connection so
    resolve_connection hands operators a fresh ACCESS_TOKEN (connected → usable)."""
    monkeypatch.setenv("OBS_CONNECTIONS_DB", str(tmp_path / "conn.db"))

    def handler(request):
        return httpx.Response(200, json={"access_token": "AT-slack", "token_type": "bearer"})

    broker = _broker(handler, monkeypatch)
    session = await broker.start_authorization(provider="github", connection_id="gh-conn")
    state = parse_qs(urlparse(session.redirect_url).query)["state"][0]
    await broker.complete_authorization(state=state, code="the-code")

    from cairn.core.connections import default_connection_store
    store = default_connection_store()
    try:
        conn = await store.get("gh-conn")
    finally:
        store.close()
    assert conn is not None and conn.kind == "oauth"
    assert conn.config == {"broker": "authlib", "provider": "github", "connection_id": "gh-conn"}

    # idempotent on re-authorization: no duplicate/overwrite
    session2 = await broker.start_authorization(provider="github", connection_id="gh-conn")
    state2 = parse_qs(urlparse(session2.redirect_url).query)["state"][0]
    await broker.complete_authorization(state=state2, code="again")
    store = default_connection_store()
    try:
        assert (await store.get("gh-conn")) is not None
    finally:
        store.close()
