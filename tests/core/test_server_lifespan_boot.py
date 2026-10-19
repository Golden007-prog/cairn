"""The server LIFESPAN must actually run under test (#1853 follow-up).

The #1853 boot check landed with `auth_enabled()` used in the lifespan but never
imported into server.py. 84 app-booting tests stayed green because they build
`TestClient(build_app(...))` WITHOUT the context manager, so the lifespan —
where the NameError lived — never executed. Prod crash-looped on first deploy.
These run the lifespan for real, with auth on, in both directions.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cairn.core.triggers import auth
from cairn.core.triggers.server import build_app
from cairn.core.engine.runner import Runner

GOOD = auth.hash_password("s3cret")
CORRUPT = "=19=65536,t=3,p=4/8HHdinpyNtId"


@pytest.fixture(autouse=True)
def _auth_on(monkeypatch, tmp_path):
    monkeypatch.setenv("OBS_JWT_SECRET", "test-secret")
    monkeypatch.setenv("OBS_API_USER", "admin")
    monkeypatch.delenv("OBS_API_PASSWORD", raising=False)
    monkeypatch.delenv("OBS_PROFILE", raising=False)
    monkeypatch.setenv("OBS_STORAGE", f"sqlite:///{tmp_path}/runs.db")


def test_lifespan_boots_with_a_parseable_hash(monkeypatch):
    monkeypatch.setenv("OBS_API_PASSWORD_HASH", GOOD)
    with TestClient(build_app(Runner())) as c:      # the context manager RUNS the lifespan
        assert c.get("/healthz").status_code == 200


def test_lifespan_refuses_a_corrupted_hash_by_name(monkeypatch):
    """#1853's actual contract, exercised end to end: auth on + unparseable
    hash → startup raises, naming the variable and the `$$` fix."""
    monkeypatch.setenv("OBS_API_PASSWORD_HASH", CORRUPT)
    with pytest.raises(RuntimeError, match="CAIRN_API_PASSWORD_HASH.*#1853"):
        with TestClient(build_app(Runner())):
            pass
