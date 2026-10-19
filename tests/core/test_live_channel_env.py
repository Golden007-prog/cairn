"""#2025 — the live identity a remote submit / a flow engine hands the box."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from cairn.core.operator.jobs import live_channel_env, live_channel_env_for


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("CAIRN_JOB_CHANNEL", "OBS_JOB_CHANNEL", "CAIRN_JOB_CHANNEL_STREAM", "OBS_JOB_CHANNEL_STREAM",
              "CAIRN_ENGINE_URL", "OBS_ENGINE_URL", "CAIRN_JWT_SECRET", "OBS_JWT_SECRET"):
        monkeypatch.delenv(k, raising=False)


def test_no_run_identity_means_nothing(monkeypatch):
    monkeypatch.setenv("CAIRN_ENGINE_URL", "http://cp")
    assert live_channel_env_for("") == {} and live_channel_env_for("p?") == {}
    assert live_channel_env(SimpleNamespace(pipeline_id="", node_name="train")) == {}


@pytest.mark.parametrize("name", ["CAIRN_ENGINE_URL", "OBS_ENGINE_URL"])
def test_http_transport_from_the_engine_url(monkeypatch, name):
    """#2429: one setting, CAIRN_ENGINE_URL, with OBS_ENGINE_URL still read as its alias."""
    monkeypatch.setenv(name, "http://cp:8000/")
    env = live_channel_env(SimpleNamespace(pipeline_id="run-1", node_name="train"))
    assert env == {"CAIRN_RUN_ID": "run-1", "CAIRN_NODE": "train", "CAIRN_ENGINE_URL": "http://cp:8000"}


def test_the_new_engine_url_wins_over_the_old(monkeypatch):
    monkeypatch.setenv("CAIRN_ENGINE_URL", "http://new")
    monkeypatch.setenv("OBS_ENGINE_URL", "http://old")
    assert live_channel_env_for("run-1", "n")["CAIRN_ENGINE_URL"] == "http://new"


@pytest.mark.parametrize("prefix", ["CAIRN_", "OBS_"])
def test_nats_channel_wins_over_the_engine_url_and_rides_under_both_names(monkeypatch, prefix):
    """#2429: the box wire carries CAIRN_JOB_CHANNEL(_STREAM) and the OBS_ twins until 1.0."""
    monkeypatch.setenv(prefix + "JOB_CHANNEL", "nats://nats:4222")
    monkeypatch.setenv(prefix + "JOB_CHANNEL_STREAM", "S")
    monkeypatch.setenv("CAIRN_ENGINE_URL", "http://cp")
    env = live_channel_env_for("run-1", "flow")
    assert env == {"CAIRN_RUN_ID": "run-1", "CAIRN_NODE": "flow",
                   "CAIRN_JOB_CHANNEL": "nats://nats:4222", "OBS_JOB_CHANNEL": "nats://nats:4222",
                   "CAIRN_JOB_CHANNEL_STREAM": "S", "OBS_JOB_CHANNEL_STREAM": "S"}


def test_a_run_scoped_bearer_is_minted_when_auth_is_on(monkeypatch):
    monkeypatch.setenv("CAIRN_ENGINE_URL", "http://cp")
    monkeypatch.setenv("CAIRN_JWT_SECRET", "a-secret-long-enough-for-hs256-please-yes")
    env = live_channel_env_for("run-9", "train")
    tok = env.get("CAIRN_ENGINE_TOKEN", "")
    assert tok.count(".") == 2  # a JWT
    import jwt as _jwt
    claims = _jwt.decode(tok, "a-secret-long-enough-for-hs256-please-yes", algorithms=["HS256"])
    assert claims["sub"] == "worker:run-9" and claims["run_id"] == "run-9"


def test_no_engine_url_and_no_nats_means_identity_only():
    assert live_channel_env_for("run-1", "n") == {"CAIRN_RUN_ID": "run-1", "CAIRN_NODE": "n"}
