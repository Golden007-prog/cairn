"""The Metaflow engine's environment allowlist (ADR-0134 D3b): core's engine-neutral set plus
Metaflow's own prefixes — and every live-channel name (#2429)."""
from __future__ import annotations

from operator_dataflow_metaflow.engines_metaflow import _ESSENTIAL_ENV as METAFLOW_ENV


def test_metaflow_forwards_its_own_configuration_and_the_contract():
    for k in ("METAFLOW_DATASTORE_SYSROOT_S3", "CAIRN_METAFLOW_PROFILE", "OBS_METAFLOW_PROFILE",
              "CAIRN_CONN_DB", "CAIRN_DATAFLOW_SPEC", "PATH"):
        assert k in METAFLOW_ENV, k
    assert "AWS_SECRET_ACCESS_KEY" not in METAFLOW_ENV and "CAIRN_STORAGE" not in METAFLOW_ENV


def test_the_metaflow_allowlist_forwards_every_live_channel_name(monkeypatch):
    """#2429: strict isolation must not strip the wire — the CAIRN_ job-channel names AND
    their OBS_ twins (for pinned box images, until 1.0) reach the task."""
    from cairn.core.operator.jobs import live_channel_env_for
    from operator_dataflow_metaflow.engines_metaflow import _ESSENTIAL_ENV

    monkeypatch.setenv("CAIRN_JOB_CHANNEL", "nats://bus:4222")
    monkeypatch.setenv("CAIRN_JOB_CHANNEL_STREAM", "S")
    nats = live_channel_env_for("run-1", "flow")
    assert {"CAIRN_JOB_CHANNEL", "OBS_JOB_CHANNEL", "CAIRN_JOB_CHANNEL_STREAM", "OBS_JOB_CHANNEL_STREAM"} <= set(nats)
    monkeypatch.delenv("CAIRN_JOB_CHANNEL")
    monkeypatch.delenv("OBS_JOB_CHANNEL", raising=False)
    monkeypatch.setenv("CAIRN_ENGINE_URL", "http://cp")
    http = live_channel_env_for("run-1", "flow")
    assert [k for k in {**nats, **http} if k not in _ESSENTIAL_ENV] == []
