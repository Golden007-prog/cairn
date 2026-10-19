"""The builtin engine vs the SHARED conformance suite (#1046/#1048).

This is the acceptance that matters: the same 9 cases every adapter
(Metaflow, SkyPilot) runs, here driven against REAL subprocesses
of the production entrypoint (cairn-dataflow-run) — CI exercises the exact
code path the SSH box runs, minus the transport."""
from __future__ import annotations

from unittest import mock

from cairn.core.dataflow import Budget, DataflowSpec, StageRef
from cairn.core.dataflow.conformance import PROBE_STAGE, DataflowEngineConformance, contract_probe

import cairn.core.dataflow.runner as runner_mod
from operator_dataflow.engines import BuiltinLocalEngine
from cairn.core.dataflow.stage_loader import load_stages


class TestBuiltinEngineConformance(DataflowEngineConformance):
    supports_cancel_midrun = True
    supports_budget_abort = True  # the runner self-enforces budget.max_seconds

    def make_engine(self) -> BuiltinLocalEngine:
        return BuiltinLocalEngine()

    def slow_spec(self) -> DataflowSpec:
        return DataflowSpec(stages=[StageRef(stage="noop.slow", config={"seconds": 20})])

    def run_interpreter(self, spec, workdir):
        """ADR-0118 D7 — the interpreter the builtin engine's subprocess runs, in-process,
        with the kit's opted-in probe kernel registered (it has no distribution version)."""
        with mock.patch.object(runner_mod, "load_stages", lambda: {**load_stages(), PROBE_STAGE: contract_probe}), \
                mock.patch.object(runner_mod, "stage_versions", lambda: {}):
            return runner_mod.run_dataflow(spec, workdir)


def test_budget_abort_is_failed_not_cancelled():
    # a budget kill must read as FAILURE (with 'budget' named), never as a user
    # cancel — the P3 budget-gate and the console distinguish the two.
    import asyncio

    from cairn.core.dataflow.conformance import poll_until_terminal

    async def go():
        engine = BuiltinLocalEngine()
        spec = DataflowSpec(
            stages=[StageRef(stage="noop"), StageRef(stage="noop.slow", config={"seconds": 20})],
            budget=Budget(max_seconds=1),
        )
        handle = await engine.submit(spec, None)
        status = await poll_until_terminal(engine, handle)
        assert status.state == "failed"
        assert "budget" in status.error.lower()
    asyncio.run(go())


# ── #2614: a job never outlives the process that owns it ─────────────────────


def test_the_runner_dies_with_its_owner(tmp_path):
    """SIGKILL the process that submitted a builtin job (a server crash): the
    runner notices it was re-parented and kills its own process group, instead of
    running on unwatched while a restarted server fails the leg 'process table
    lost'. A REAL parent process, a REAL runner subprocess, a REAL SIGKILL."""
    import os
    import signal
    import subprocess
    import sys
    import time

    owner = tmp_path / "owner.py"
    owner.write_text(
        "import asyncio, sys\n"
        "from cairn.core.dataflow import DataflowSpec, StageRef\n"
        "from operator_dataflow.engines import BuiltinLocalEngine\n"
        "async def go():\n"
        "    h = await BuiltinLocalEngine().submit(\n"
        "        DataflowSpec(stages=[StageRef(stage='noop.slow', config={'seconds': 60})]), None)\n"
        "    print(h.meta['pid'], flush=True)\n"
        "    await asyncio.sleep(120)\n"
        "asyncio.run(go())\n"
    )
    parent = subprocess.Popen([sys.executable, str(owner)], stdout=subprocess.PIPE, text=True)
    try:
        runner_pid = int(parent.stdout.readline())
        os.kill(runner_pid, 0)  # the job is running under its owner
        time.sleep(1.0)
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=10)
        deadline = time.time() + 10
        alive = True
        while time.time() < deadline:
            try:
                os.kill(runner_pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.1)
        assert not alive, "the runner outlived its owner — an orphaned job (#2614)"
    finally:
        if parent.poll() is None:
            parent.kill()


def test_the_runner_without_an_owner_is_not_watched(monkeypatch):
    """Only the local engine sets the owner variable: the same entrypoint on an SSH
    box or Metaflow starts no watchdog."""
    from operator_dataflow.engine import entry

    started = []
    monkeypatch.setattr(entry, "watch_owner", lambda pid, **kw: started.append(pid))
    monkeypatch.delenv("CAIRN_DATAFLOW_OWNER_PID", raising=False)
    monkeypatch.setenv("CAIRN_DATAFLOW_SPEC", '{"stages": [{"stage": "noop"}]}')
    monkeypatch.chdir(__import__("tempfile").mkdtemp())
    assert entry.main([]) == 0
    assert started == []
