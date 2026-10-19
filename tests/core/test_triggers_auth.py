"""JWT auth for the trigger server (an internal issue).

Unit: token round-trip, password auth, the enable toggle.
Server: control-plane routes 401 without a token when enabled and pass with a
valid one; /healthz + /token stay open; disabled mode (no secret) is open.
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest
from fastapi.testclient import TestClient

from cairn.core.engine.runner import Runner
from cairn.core.triggers import auth
from cairn.core.triggers.server import build_app


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch):
    for k in (
        "OBS_JWT_SECRET", "OBS_JWT_ALGORITHM", "OBS_ACCESS_TOKEN_EXPIRE_MINUTES",
        "OBS_API_USER", "OBS_API_PASSWORD", "OBS_API_PASSWORD_HASH", "OBS_REQUIRE_AUTH",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


# ─── #391: OBS_REQUIRE_AUTH refuses an open control plane ──────────────


def test_install_auth_refuses_when_required_but_no_secret(monkeypatch):
    from fastapi import FastAPI
    monkeypatch.setenv("OBS_REQUIRE_AUTH", "1")
    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="CAIRN_REQUIRE_AUTH"):
        auth.install_auth(FastAPI())


def test_install_auth_ok_when_required_and_secret_set(monkeypatch):
    from fastapi import FastAPI
    monkeypatch.setenv("OBS_REQUIRE_AUTH", "1")
    monkeypatch.setenv("OBS_JWT_SECRET", "s3cret")
    auth.install_auth(FastAPI())  # must NOT raise — auth is on


def test_install_auth_only_warns_when_not_required(monkeypatch):
    from fastapi import FastAPI
    monkeypatch.delenv("OBS_REQUIRE_AUTH", raising=False)
    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)
    auth.install_auth(FastAPI())  # keyless dev stays open (warn, no raise)


# ─── console SPA must stay reachable when auth is ON (first auth-deploy bug) ──


def test_is_public_path_allows_console_static_not_api():
    assert auth._is_public_path("/console")
    assert auth._is_public_path("/console/assets/index.js")
    assert auth._is_public_path("/healthz")
    assert not auth._is_public_path("/console/api/runs")  # read-API is protected
    assert not auth._is_public_path("/runs")              # flat routes protected


def test_console_spa_reachable_but_api_protected_with_auth_on(monkeypatch):
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret")
    client = TestClient(_app())
    # The SPA + assets must NOT 401 (so the browser can load the login page).
    # They may 404 if dist isn't built in the test env — the point is "not 401".
    assert client.get("/console").status_code != 401
    assert client.get("/console/assets/app.js").status_code != 401
    # The console read-API stays behind auth.
    assert client.get("/console/api/runs").status_code == 401


# ─── unit ──────────────────────────────────────────────────────────


def test_auth_disabled_without_secret():
    assert auth.auth_enabled() is False


def test_token_roundtrip(monkeypatch):
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret")
    tok = auth.create_access_token({"sub": "alice"})
    assert auth.decode_subject(tok) == "alice"


def test_decode_rejects_garbage(monkeypatch):
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret")
    assert auth.decode_subject("not.a.jwt") is None


def test_decode_rejects_wrong_secret(monkeypatch):
    monkeypatch.setenv("OBS_JWT_SECRET", "secret-a")
    tok = auth.create_access_token({"sub": "alice"})
    monkeypatch.setenv("OBS_JWT_SECRET", "secret-b")
    assert auth.decode_subject(tok) is None


def test_decode_rejects_expired(monkeypatch):
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret")
    tok = auth.create_access_token({"sub": "alice"}, expires_delta=timedelta(seconds=-1))
    assert auth.decode_subject(tok) is None


def test_authenticate_user_password(monkeypatch):
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret")
    monkeypatch.setenv("OBS_API_USER", "admin")
    monkeypatch.setenv("OBS_API_PASSWORD", "s3cret")
    assert auth.authenticate_user("admin", "s3cret") == "admin"
    assert auth.authenticate_user("admin", "wrong") is None
    assert auth.authenticate_user("eve", "s3cret") is None


def test_authenticate_user_with_hash(monkeypatch):
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret")
    monkeypatch.setenv("OBS_API_USER", "admin")
    monkeypatch.setenv("OBS_API_PASSWORD_HASH", auth.hash_password("s3cret"))
    assert auth.authenticate_user("admin", "s3cret") == "admin"
    assert auth.authenticate_user("admin", "nope") is None


# ─── server integration ────────────────────────────────────────────


def _app():
    return build_app(Runner(), enable_scheduler=False, enable_spec_routes=False)


def test_open_when_auth_disabled():
    client = TestClient(_app())
    assert client.get("/healthz").status_code == 200
    # control-plane route reachable without a token
    assert client.get("/runs").status_code == 200


def test_protected_when_enabled(monkeypatch):
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret")
    monkeypatch.setenv("OBS_API_USER", "admin")
    monkeypatch.setenv("OBS_API_PASSWORD", "s3cret")
    client = TestClient(_app())

    # health stays open
    assert client.get("/healthz").status_code == 200
    # control plane blocked without a token
    assert client.get("/runs").status_code == 401

    # wrong creds → 401
    assert client.post("/token", data={"username": "admin", "password": "wrong"}).status_code == 401

    # good creds → token → control plane opens
    tok = client.post("/token", data={"username": "admin", "password": "s3cret"})
    assert tok.status_code == 200
    token = tok.json()["access_token"]
    assert client.get("/runs", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_bad_token_rejected(monkeypatch):
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret")
    monkeypatch.setenv("OBS_API_PASSWORD", "s3cret")
    client = TestClient(_app())
    assert client.get("/runs", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_token_endpoint_503_when_enabled_but_no_admin(monkeypatch, tmp_path):
    # secret set but no OBS_API_PASSWORD[_HASH] configured — AND no user store.
    # The store must be pinned to an EMPTY location (#1199): unpinned, the check
    # walked to the developer's real ~/.cairn accounts db (populated → not 503).
    # This only "passed" before because the repo .env pointed OBS_STORAGE at an
    # unreachable postgres, which errored into the same False.
    import cairn.core.account as account

    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret")
    monkeypatch.setenv("OBS_ACCOUNT_DB", str(tmp_path / "accounts.db"))
    monkeypatch.setattr(account, "_store", None)  # drop the process-wide cache
    client = TestClient(_app())
    resp = client.post("/token", data={"username": "admin", "password": "x"})
    assert resp.status_code == 503


# ─── #2536: a base install without the `auth` extra answers 501, not 500 ──


@pytest.fixture
def _no_auth_extra(monkeypatch, tmp_path):
    """The `cairn dev` shape (auth on, dev/dev env admin, an empty user store)
    on an install where `pwdlib` — the `auth` extra — is absent."""
    import cairn.core.account as account
    from cairn.core import auth_tokens

    monkeypatch.setitem(sys.modules, "pwdlib", None)  # `from pwdlib import …` → ImportError
    monkeypatch.setattr(auth_tokens, "_PH", None)
    monkeypatch.setattr(auth_tokens, "_DUMMY_HASH", None)
    monkeypatch.setenv("OBS_JWT_SECRET", "cairn-dev-secret")
    monkeypatch.setenv("OBS_API_USER", "dev")
    monkeypatch.setenv("OBS_API_PASSWORD", "dev")
    monkeypatch.setenv("OBS_ACCOUNT_DB", str(tmp_path / "accounts.db"))
    monkeypatch.setattr(account, "_store", None)
    return TestClient(_app(), raise_server_exceptions=False)


def test_token_without_the_auth_extra_is_501_naming_the_extra(_no_auth_extra):
    resp = _no_auth_extra.post("/token", data={"username": "dev", "password": "dev"})
    assert resp.status_code == 501, resp.text
    assert "cairndev[auth]" in resp.json()["detail"]


def test_signup_without_the_auth_extra_is_501(_no_auth_extra):
    resp = _no_auth_extra.post("/auth/signup", json={
        "company": "Acme", "email": "a@acme.test", "password": "pw"})
    assert resp.status_code == 501, resp.text
    assert "cairndev[auth]" in resp.json()["detail"]
