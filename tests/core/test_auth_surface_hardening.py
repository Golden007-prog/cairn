"""Auth-surface hardening (#1507).

Three separate holes that share a shape — the control plane was more reachable,
and more describable, than it needed to be:

1. **Bind.** `cairn-server` hardcoded ``host="0.0.0.0"``. Combined with the
   fail-open default (auth enforced only when ``OBS_JWT_SECRET`` is set), a
   keyless start published an OPEN control plane on every interface. Being
   unauthenticated on loopback is `cairn dev`; being unauthenticated on the
   network is an incident.
2. **Docs.** ``/docs`` + ``/openapi.json`` + ``/redoc`` were open pre-auth even
   in production, publishing the whole route and schema map.
3. **Login throttle.** ``POST /token`` had no lockout, so an exposed port was a
   password oracle running at request speed.
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest
from fastapi.testclient import TestClient

from cairn.core import account as acct
from cairn.core.account import SqliteAccountStore
from cairn.core.engine.runner import Runner
from cairn.core.triggers import auth as auth_mod
from cairn.core.triggers.server import build_app


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setenv("OBS_STORAGE", "")
    for k in ("OBS_API_USER", "OBS_API_PASSWORD", "OBS_API_PASSWORD_HASH"):
        monkeypatch.delenv(k, raising=False)
    store = SqliteAccountStore(path=str(tmp_path / "accounts.db"))
    monkeypatch.setattr(acct, "_store", store)
    auth_mod._login_failures.clear()
    yield store
    auth_mod._login_failures.clear()
    monkeypatch.setattr(acct, "_store", None)
    store.close()


def _app():
    return build_app(Runner(), enable_scheduler=False, enable_spec_routes=False)


def _seed_user(store, username="root", password="root-pw", role="owner"):
    async def _seed():
        user = await acct.create_user(username, password, store=store)
        await acct.add_member("default", user.id, role, store=store)
        return user

    return asyncio.run(_seed())


# ─── 1. an unauthenticated server must not bind to the world ───────────


def test_keyless_server_binds_loopback_only(monkeypatch):
    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)
    monkeypatch.delenv("OBS_TRIGGER_HOST", raising=False)
    captured = {}

    def _fake_run(app, host, port, **kw):
        captured["host"] = host

    _run_main(monkeypatch, _fake_run)
    assert captured["host"] == "127.0.0.1", "keyless server must not be network-reachable"


def test_authenticated_server_binds_all_interfaces(monkeypatch):
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret")
    monkeypatch.delenv("OBS_TRIGGER_HOST", raising=False)
    captured = {}

    def _fake_run(app, host, port, **kw):
        captured["host"] = host

    _run_main(monkeypatch, _fake_run)
    assert captured["host"] == "0.0.0.0", "auth-on is the deployed case — must stay reachable"


def test_the_bind_is_overridable(monkeypatch):
    """A container without auth still needs a way out — but the operator has to
    ask for it, rather than getting it silently."""
    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)
    monkeypatch.setenv("OBS_TRIGGER_HOST", "0.0.0.0")
    captured = {}

    def _fake_run(app, host, port, **kw):
        captured["host"] = host

    _run_main(monkeypatch, _fake_run)
    assert captured["host"] == "0.0.0.0"


def _run_main(monkeypatch, fake_run):
    """Drive the real entrypoint with uvicorn.run stubbed out."""
    import uvicorn

    from cairn.core.triggers import main as main_mod

    from pathlib import Path

    import cairn.profiles as profiles_pkg

    monkeypatch.setenv("OBS_PROFILE", str(Path(profiles_pkg.__file__).parent / "local-dev.toml"))  # #2296
    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setattr(main_mod, "build_default_runner", lambda: Runner())
    main_mod.main()


# ─── 2. docs describe the control plane — gate them when auth is on ────


def test_docs_are_closed_when_auth_is_enabled(monkeypatch):
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret")
    _seed_user(acct.get_account_store())
    client = TestClient(_app())
    for path in ("/docs", "/openapi.json", "/redoc"):
        assert client.get(path).status_code == 401, f"{path} leaks the route map pre-auth"


def test_docs_stay_open_in_keyless_dev(monkeypatch):
    """They are the fastest way to learn the surface — and there is nothing to
    protect when the whole plane is already open."""
    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)
    client = TestClient(_app())
    assert client.get("/openapi.json").status_code == 200


def test_health_and_login_stay_public(monkeypatch):
    """Gating docs must not gate the things a load balancer and a login page
    need before they hold a token."""
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret")
    _seed_user(acct.get_account_store())
    client = TestClient(_app())
    assert client.get("/healthz").status_code == 200
    assert client.get("/auth/status").status_code == 200


# ─── 3. POST /token throttle ───────────────────────────────────────────


def test_repeated_failures_lock_out_then_recover(monkeypatch):
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret")
    monkeypatch.setattr(auth_mod, "_LOGIN_MAX_FAILURES", 3)
    monkeypatch.setattr(auth_mod, "_LOGIN_LOCKOUT_SECONDS", 60.0)
    _seed_user(acct.get_account_store())
    client = TestClient(_app())

    for _ in range(3):
        assert client.post("/token", data={"username": "root", "password": "wrong"}).status_code == 401
    blocked = client.post("/token", data={"username": "root", "password": "wrong"})
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1

    # Even the CORRECT password is refused while locked out — otherwise the
    # throttle is trivially bypassed by the guess that happens to be right.
    assert client.post("/token", data={"username": "root", "password": "root-pw"}).status_code == 429

    # The window expiring restores service.
    monkeypatch.setattr(auth_mod, "_LOGIN_LOCKOUT_SECONDS", 0.0)
    assert client.post("/token", data={"username": "root", "password": "root-pw"}).status_code == 200


def test_a_success_clears_the_counter(monkeypatch):
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret")
    monkeypatch.setattr(auth_mod, "_LOGIN_MAX_FAILURES", 3)
    _seed_user(acct.get_account_store())
    client = TestClient(_app())

    for _ in range(2):
        client.post("/token", data={"username": "root", "password": "wrong"})
    assert client.post("/token", data={"username": "root", "password": "root-pw"}).status_code == 200
    # counter reset — two more failures must not trip the limit
    for _ in range(2):
        assert client.post("/token", data={"username": "root", "password": "wrong"}).status_code == 401


def test_one_account_lockout_does_not_lock_out_another(monkeypatch):
    """A username-only counter would let an attacker deny login to every user
    by guessing at them. The key includes the client address."""
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret")
    monkeypatch.setattr(auth_mod, "_LOGIN_MAX_FAILURES", 2)
    store = acct.get_account_store()
    _seed_user(store)
    _seed_user(store, username="second", password="second-pw", role="member")
    client = TestClient(_app())

    for _ in range(2):
        client.post("/token", data={"username": "root", "password": "wrong"})
    assert client.post("/token", data={"username": "root", "password": "wrong"}).status_code == 429
    # a different account from the same client is unaffected
    assert client.post("/token", data={"username": "second", "password": "second-pw"}).status_code == 200
