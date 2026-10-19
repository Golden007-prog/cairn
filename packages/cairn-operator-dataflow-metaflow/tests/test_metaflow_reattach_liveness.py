"""#1429 — a RE-ATTACHED local Metaflow job whose bridge process is gone (the
worker restart killed the child too) and whose files never reached a terminal
state used to poll ``running`` until the leg's poll budget expired — a 7-day
hang for a dead job. Now the persisted pgid is checked (procfs first: the prod
image has no ``ps``): confirmed-gone → ``failed`` + ``retriable`` (H2 retry
resubmits from the last durable checkpoint); alive-and-ours → still running;
unknowable → still running (never a false death)."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from cairn.core.dataflow import EngineJobHandle

from operator_dataflow_metaflow import engines_metaflow as M
from operator_dataflow_metaflow.engines_metaflow import MetaflowEngine, _bridge_process_state


def _handle(wd: Path, pid: int) -> EngineJobHandle:
    return EngineJobHandle(engine="metaflow", job_id=f"mf-{pid}",
                           meta={"workdir": str(wd), "pid": pid, "pgid": pid})


#: The bridge as a NEW job runs it, and as a job launched before the engine left
#: cairn-operator-dataflow runs it (ADR-0134 D2): both must be recognised for one release.
@pytest.fixture(params=["operator_dataflow_metaflow.metaflow_bridge", "operator_dataflow.metaflow_bridge"],
                ids=["current", "pre-upgrade"])
def bridge_like(request):
    wd = Path(tempfile.mkdtemp(prefix="cairn-mf-live-"))
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import sys,time\nsys.argv[0]='" + request.param + "'\ntime.sleep(120)",
         "-m", request.param, "--workdir", str(wd)],
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.4)
    yield proc, wd
    if proc.poll() is None:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait()


def test_reattached_dead_process_no_terminal_marker_is_failed_retriable():
    wd = Path(tempfile.mkdtemp(prefix="cairn-mf-dead-"))
    (wd / "progress.json").write_text(json.dumps({"state": "running", "stage": "pii.redact",
                                                  "stages_done": 2, "stages_total": 5}))
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    M._PROCS.pop(f"mf-{dead.pid}", None)          # nothing live in this process
    st = asyncio.run(MetaflowEngine().poll(_handle(wd, dead.pid)))
    assert st.state == "failed" and st.retriable is True
    assert "gone" in (st.error or "") and "checkpoint" in (st.error or "")
    assert st.metrics.get("stages_done") == 2      # progress carried for the record


def test_reattached_live_bridge_process_is_still_running(bridge_like):
    proc, wd = bridge_like
    (wd / "progress.json").write_text(json.dumps({"state": "running", "stage": "dedup"}))
    st = asyncio.run(MetaflowEngine().poll(_handle(wd, proc.pid)))
    assert st.state == "running"


def test_reattached_terminal_marker_wins_over_liveness():
    """A job that FINISHED and whose process exited normally: the file is the
    truth (`succeeded`), the pid being gone is expected, not a failure."""
    wd = Path(tempfile.mkdtemp(prefix="cairn-mf-fin-"))
    (wd / "progress.json").write_text(json.dumps({"state": "succeeded"}))
    dead = subprocess.Popen([sys.executable, "-c", "pass"]); dead.wait()
    st = asyncio.run(MetaflowEngine().poll(_handle(wd, dead.pid)))
    assert st.state == "succeeded"


def test_pre_1348_handle_without_pgid_stays_running():
    wd = Path(tempfile.mkdtemp(prefix="cairn-mf-old-"))
    h = EngineJobHandle(engine="metaflow", job_id="mf-legacy", meta={"workdir": str(wd)})
    assert asyncio.run(MetaflowEngine().poll(h)).state == "running"


def test_process_state_tristate(bridge_like):
    proc, wd = bridge_like
    assert asyncio.run(_bridge_process_state(proc.pid, wd)) is True
    assert asyncio.run(_bridge_process_state(proc.pid, Path("/elsewhere"))) is False   # recycled-pid shape
    dead = subprocess.Popen([sys.executable, "-c", "pass"]); dead.wait()
    assert asyncio.run(_bridge_process_state(dead.pid, wd)) is False


def test_liveness_never_uses_ps_when_procfs_exists(monkeypatch, bridge_like):
    """The prod image (python:3.13-slim) has no `ps`; procfs is the primary path."""
    proc, wd = bridge_like
    if not Path("/proc").is_dir():
        pytest.skip("no procfs on this host — the ps fallback is exercised instead")

    async def _boom(*a, **k):  # pragma: no cover — must not be reached
        raise AssertionError("ps must not be spawned when /proc exists")
    monkeypatch.setattr(M.asyncio, "create_subprocess_exec", _boom)
    assert asyncio.run(_bridge_process_state(proc.pid, wd)) is True
