"""#1879 — /auth/me carries the resolved feature map the console renders from."""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from fastapi.testclient import TestClient

from cairn.core.engine.runner import Runner
from cairn.core.features import FEATURE_IDS, LIGHT_OFF
from cairn.core.triggers.server import build_app


def _me(monkeypatch, **env):
    for k in ("OBS_LIGHT", *[f"OBS_FEATURE_{n.upper()}" for n in FEATURE_IDS]):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)   # keyless → full-access identity
    with TestClient(build_app(Runner())) as c:
        r = c.get("/auth/me")
    assert r.status_code == 200, r.text
    return r.json()


def test_auth_me_carries_every_feature_on_by_default(monkeypatch):
    body = _me(monkeypatch)
    assert body["is_admin"] is True
    assert set(body["features"]) == set(FEATURE_IDS)
    assert all(body["features"].values())


def test_auth_me_reflects_the_light_preset_and_overrides(monkeypatch):
    body = _me(monkeypatch, OBS_LIGHT="on", OBS_FEATURE_JUDGES="on")
    f = body["features"]
    assert {k for k, v in f.items() if not v} == set(LIGHT_OFF) - {"judges"}
