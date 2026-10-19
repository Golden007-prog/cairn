"""The reusable ``require_auth`` FastAPI dependency (an internal issue).

``require_auth`` lets a route/router opt into bearer-token auth while reusing
``auth.decode_subject`` for verification and composing with the global
``install_auth`` middleware. It must fail OPEN exactly like that middleware:
no ``OBS_JWT_SECRET`` → no enforcement (keyless dev + existing tests unchanged);
secret + admin configured → 401 without a token, 200 with a valid one obtained
from ``POST /token``.

Driven over ``build_app(FakeRunner(), enable_scheduler=False)`` with a hermetic
sqlite store (the dev .env forces postgres, which isn't reachable in CI).
"""
from __future__ import annotations

import os
import types

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from cairn.core.triggers import auth
from cairn.core.triggers.auth import require_auth
from cairn.core.triggers.server import build_app


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch, tmp_path):
    # Hermetic run store: the dev .env points OBS_STORAGE at postgres, which
    # isn't reachable here (Connect call failed ...:5432). Force sqlite.
    monkeypatch.setenv("OBS_STORAGE", f"sqlite://{tmp_path}/runs.db")
    # Start every test from a clean auth slate so secret/password leakage from
    # the process env can't flip the enable toggle.
    for k in (
        "OBS_JWT_SECRET", "OBS_JWT_ALGORITHM", "OBS_ACCESS_TOKEN_EXPIRE_MINUTES",
        "OBS_API_USER", "OBS_API_PASSWORD", "OBS_API_PASSWORD_HASH",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


class FakeRunner:
    """Minimal Runner stand-in — no LLM, no real backend."""

    def __init__(self):
        self.default_environment = "production"
        self.skill_engine = types.SimpleNamespace(
            list_skills=lambda: ["db-pool"],
            list_skills_detail=lambda: [{"name": "db-pool"}],
        )

    async def run(self, event, *, environment=None, event_sink=None, run_id=None, _cascade_depth=0):
        return []


def _app():
    return build_app(FakeRunner(), enable_scheduler=False, enable_spec_routes=False)


# ─── (a) auth disabled → dependency is a no-op ─────────────────────


def test_require_auth_returns_none_when_disabled():
    # Direct unit check of the fail-open contract.
    assert require_auth(token=None) is None
    assert require_auth(token="anything") is None


def test_protected_route_open_when_no_secret():
    client = TestClient(_app())
    # control-plane routes carrying require_auth stay reachable without a token
    assert client.get("/runs").status_code == 200
    assert client.get("/runs/run-missing").status_code == 404  # not 401
    # health + login surface stay open too
    assert client.get("/healthz").status_code == 200


# ─── (b) auth enabled → 401 without a token, 200 with a valid one ──


def test_protected_route_requires_token_when_enabled(monkeypatch):
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret")
    monkeypatch.setenv("OBS_API_USER", "admin")
    monkeypatch.setenv("OBS_API_PASSWORD", "s3cret")
    client = TestClient(_app())

    # health + login stay reachable without a token
    assert client.get("/healthz").status_code == 200

    # protected control-plane route blocked without a token
    assert client.get("/runs").status_code == 401
    assert client.get("/runs/run-x").status_code == 401

    # obtain a token from POST /token, then the route opens
    tok = client.post("/token", data={"username": "admin", "password": "s3cret"})
    assert tok.status_code == 200
    token = tok.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/runs", headers=headers).status_code == 200
    # unknown run → 404 (passed auth), not 401
    assert client.get("/runs/run-missing", headers=headers).status_code == 404


def test_protected_route_rejects_bad_token(monkeypatch):
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret")
    monkeypatch.setenv("OBS_API_PASSWORD", "s3cret")
    client = TestClient(_app())
    assert client.get("/runs", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_require_auth_dependency_in_isolation(monkeypatch):
    """The dependency works standalone on any FastAPI app, not just build_app —
    proves it's reusable (no global middleware involved here)."""
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret")

    app = FastAPI()

    @app.get("/guarded")
    def guarded(subject: str = Depends(require_auth)) -> dict:
        return {"subject": subject}

    client = TestClient(app)
    assert client.get("/guarded").status_code == 401

    token = auth.create_access_token({"sub": "alice"})
    resp = client.get("/guarded", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["subject"] == "alice"
