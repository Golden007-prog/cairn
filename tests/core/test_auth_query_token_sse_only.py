"""The query-string bearer is for the run SSE stream only (#58).

A browser's ``EventSource`` cannot set an ``Authorization`` header (#967), so the
bearer guard accepts ``?access_token=<jwt>`` as a fallback. It must accept it on
the stream route and nowhere else: on any other route a token in the URL lands in
proxy access logs, browser history and ``Referer`` headers for no reason.

Driven through the real app (``build_app`` + ``install_auth``) with auth on.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest
from fastapi.testclient import TestClient

from cairn.core.engine.runner import Runner
from cairn.core.triggers import auth
from cairn.core.triggers.server import build_app


@pytest.fixture
def client(monkeypatch):
    for k in ("OBS_JWT_ALGORITHM", "OBS_ACCESS_TOKEN_EXPIRE_MINUTES", "OBS_REQUIRE_AUTH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret")
    app = build_app(Runner(), enable_scheduler=False, enable_spec_routes=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def token(client) -> str:
    return auth.create_access_token({"sub": "alice"})


# ─── the defect: a query token authenticated ordinary API calls ─────────


@pytest.mark.parametrize("method,path", [
    ("GET", "/runs/no-such-run/events"),             # JSON read, not a stream
    ("POST", "/runs/no-such-run/cancel"),            # a mutation
    ("GET", "/workspaces/default/runs/no-such-run/events"),
])
def test_query_token_is_refused_off_the_stream_route(client, token, method, path):
    r = client.request(method, f"{path}?access_token={token}")
    assert r.status_code == 401, (r.status_code, r.text)


def test_query_token_cannot_start_a_run(client, token):
    r = client.post(f"/triggers/manual?access_token={token}", json={"skill_name": "db-pool", "wait": False})
    assert r.status_code == 401, (r.status_code, r.text)


# ─── the legitimate paths keep working ─────────────────────────────────


@pytest.mark.parametrize("path", [
    "/runs/no-such-run/stream",
    "/workspaces/default/runs/no-such-run/stream",   # the console's form
])
def test_query_token_still_authenticates_the_sse_stream(client, token, path):
    # 404 (no such run) is the route answering: the guard let the token through.
    r = client.get(f"{path}?access_token={token}")
    assert r.status_code == 404, (r.status_code, r.text)


def test_stream_without_any_token_is_still_401(client):
    assert client.get("/runs/no-such-run/stream").status_code == 401


def test_stream_with_a_bad_query_token_is_401(client):
    assert client.get("/runs/no-such-run/stream?access_token=garbage").status_code == 401


def test_query_token_on_the_stream_path_is_get_only(client, token):
    r = client.post(f"/runs/no-such-run/stream?access_token={token}")
    assert r.status_code == 401, (r.status_code, r.text)


def test_header_token_still_works_everywhere(client, token):
    h = {"Authorization": f"Bearer {token}"}
    assert client.get("/runs/no-such-run/events", headers=h).status_code == 404
    assert client.get("/runs/no-such-run/stream", headers=h).status_code == 404


def test_keyless_dev_is_unchanged(monkeypatch):
    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)
    app = build_app(Runner(), enable_scheduler=False, enable_spec_routes=False)
    with TestClient(app) as c:
        assert c.get("/runs/no-such-run/events").status_code == 404
