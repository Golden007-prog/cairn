"""#2025 — the builtin engine hands a flow subprocess the live-channel identity
so a stage kernel can stream its curve back to the control plane."""
from __future__ import annotations

import asyncio

import pytest

from cairn.core.dataflow import DataflowSpec, StageRef

from operator_dataflow.engines import BuiltinLocalEngine


class _Proc:
    pid = 4242
    returncode = None

    async def wait(self):
        return 0


@pytest.mark.asyncio
async def test_builtin_submit_env_carries_the_live_identity(monkeypatch):
    captured: dict = {}

    async def fake_exec(*argv, **kw):
        captured.update(kw)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.delenv("OBS_JOB_CHANNEL", raising=False)
    monkeypatch.delenv("CAIRN_JOB_CHANNEL", raising=False)
    monkeypatch.delenv("OBS_JWT_SECRET", raising=False)
    monkeypatch.setenv("OBS_ENGINE_URL", "http://127.0.0.1:8000")
    spec = DataflowSpec(dataflow_ref="training/finetune", stages=[StageRef(stage="noop")],
                        config={}, parent_run="run-flow-7")
    eng = BuiltinLocalEngine()
    monkeypatch.setattr(eng, "validate", lambda s: [])
    await eng.submit(spec, None)
    env = captured["env"]
    assert env["CAIRN_PARENT_RUN"] == "run-flow-7"
    assert env["CAIRN_RUN_ID"] == "run-flow-7" and env["CAIRN_NODE"] == "flow"
    assert env["CAIRN_ENGINE_URL"] == "http://127.0.0.1:8000"
