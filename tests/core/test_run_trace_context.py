"""#1465 — run identity on LLM spans: contextvar first, env chain second."""
from __future__ import annotations

from cairn.core.obs import telemetry


def test_env_chain(monkeypatch):
    monkeypatch.delenv("CAIRN_RUN_ID", raising=False)
    monkeypatch.delenv("CAIRN_PARENT_RUN", raising=False)
    assert telemetry.current_run_id() == ""
    monkeypatch.setenv("CAIRN_PARENT_RUN", "run-parent")
    assert telemetry.current_run_id() == "run-parent"
    monkeypatch.setenv("CAIRN_RUN_ID", "run-direct")
    assert telemetry.current_run_id() == "run-direct"


def test_contextvar_wins_and_resets(monkeypatch):
    monkeypatch.setenv("CAIRN_RUN_ID", "run-env")
    token = telemetry.set_current_run("run-ctx")
    try:
        assert telemetry.current_run_id() == "run-ctx"
    finally:
        telemetry._current_run.reset(token)
    assert telemetry.current_run_id() == "run-env"
