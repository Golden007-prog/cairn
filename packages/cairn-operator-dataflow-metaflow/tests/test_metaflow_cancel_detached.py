"""#1348 (ADR-0089) — cancel must reach a job this process did not start.

`_PROCS` is in-memory: after a control-plane restart it is empty while the
detached child (``start_new_session=True``) keeps computing. `cancel` used to
`return` on the missing entry — reporting success while killing nothing, so a
run went `cancelled` in cairn with its GPU still burning on the box.

The load-bearing test here is `test_a_detached_process_is_actually_killed`: it
spawns a REAL orphan in its own process group, wipes `_PROCS` to simulate the
restart, and asserts the process is gone afterwards. The rest pin the safety
gate — a recorded pid may have been RECYCLED, and killing it blind would be a
worse bug than the one being fixed.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from cairn.core.dataflow import EngineJobHandle

from operator_dataflow_metaflow.engines_metaflow import _PROCS, MetaflowEngine, _is_our_bridge


def _alive(proc: subprocess.Popen) -> bool:
    """Is the child still RUNNING?

    ``os.kill(pid, 0)`` is not the right question for a child of this process:
    a killed-but-unreaped child is a zombie, whose pid still accepts signal 0 —
    which made this suite report a correctly-killed process as alive.
    ``poll()`` reaps and answers truthfully.
    """
    return proc.poll() is None


#: The bridge as a NEW job runs it, and as a job launched before the engine left
#: cairn-operator-dataflow runs it (ADR-0134 D2): both must be recognised for one release.
@pytest.fixture(params=["operator_dataflow_metaflow.metaflow_bridge", "operator_dataflow.metaflow_bridge"],
                ids=["current", "pre-upgrade"])
def orphan(request):
    """A real detached process whose argv looks like the bridge — same shape the
    engine spawns (own session → own process group)."""
    wd = Path(tempfile.mkdtemp(prefix="cairn-mf-detached-"))
    # argv must satisfy the identity gate: bridge module + this job's workdir.
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import sys,time\n"
         "sys.argv[0]='" + request.param + "'\n"
         "time.sleep(120)",
         "-m", request.param, "--workdir", str(wd)],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.4)  # let it reach the sleep
    yield proc, wd
    if _alive(proc):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def test_a_detached_process_is_actually_killed(orphan):
    """The bug: cancel returned success while the child kept running."""
    proc, wd = orphan
    assert _alive(proc), "fixture process should be running"
    _PROCS.pop(f"mf-{proc.pid}", None)  # the restart: no live entry

    handle = EngineJobHandle(engine="metaflow", job_id=f"mf-{proc.pid}",
                             meta={"workdir": str(wd), "pid": proc.pid, "pgid": proc.pid})
    asyncio.run(MetaflowEngine().cancel(handle))

    deadline = time.time() + 15
    while time.time() < deadline and _alive(proc):
        time.sleep(0.2)
    assert not _alive(proc), "detached job survived cancel — #1348 regression"


def test_cancel_marks_progress_so_poll_reports_cancelled(orphan):
    """File-truth is what a re-attached poll reads (no Process object exists)."""
    proc, wd = orphan
    _PROCS.pop(f"mf-{proc.pid}", None)
    handle = EngineJobHandle(engine="metaflow", job_id=f"mf-{proc.pid}",
                             meta={"workdir": str(wd), "pid": proc.pid, "pgid": proc.pid})
    eng = MetaflowEngine()
    asyncio.run(eng.cancel(handle))
    assert asyncio.run(eng.poll(handle)).state == "cancelled"


# ── the safety gate: never signal an unverified pid ──────────────────────────

def test_a_recycled_pid_is_left_alone():
    """A pid whose argv is NOT our bridge must never be signalled — killing an
    unrelated process is worse than the no-op this fix replaces."""
    innocent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.3)
    try:
        wd = Path(tempfile.mkdtemp(prefix="cairn-mf-other-"))
        handle = EngineJobHandle(engine="metaflow", job_id=f"mf-{innocent.pid}",
                                 meta={"workdir": str(wd), "pid": innocent.pid,
                                       "pgid": innocent.pid})
        asyncio.run(MetaflowEngine().cancel(handle))
        time.sleep(0.5)
        assert _alive(innocent), "cancel killed a process that was not its job"
    finally:
        try:
            os.killpg(os.getpgid(innocent.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def test_identity_gate_requires_both_module_and_workdir(orphan):
    proc, wd = orphan
    assert asyncio.run(_is_our_bridge(proc.pid, wd)) is True
    # right module, WRONG job → not ours (workdir is the per-job discriminator)
    other = Path(tempfile.mkdtemp(prefix="cairn-mf-elsewhere-"))
    assert asyncio.run(_is_our_bridge(proc.pid, other)) is False


def test_a_dead_pid_is_not_our_bridge():
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    assert asyncio.run(_is_our_bridge(dead.pid, Path("/tmp"))) is False


def test_a_pre_1348_handle_without_pgid_is_a_safe_noop():
    """Handles persisted before this fix carry no pgid — cancel must not raise."""
    handle = EngineJobHandle(engine="metaflow", job_id="mf-1",
                             meta={"workdir": "/tmp/whatever"})
    asyncio.run(MetaflowEngine().cancel(handle))  # no exception = pass


def test_submit_records_the_pgid_for_later_reattach():
    """Without this the fix has nothing durable to work from."""
    import operator_dataflow_metaflow.engines_metaflow as em

    class _FakeProc:
        pid = 4242
        returncode = None

    async def _fake_exec(*a, **kw):
        return _FakeProc()

    async def go():
        eng = MetaflowEngine()
        eng.validate = lambda spec: []  # skip registry/metaflow availability
        orig = asyncio.create_subprocess_exec
        asyncio.create_subprocess_exec = _fake_exec
        try:
            from cairn.core.dataflow import DataflowSpec
            spec = DataflowSpec.model_validate({"stages": [{"stage": "x"}]})
            return await eng.submit(spec, None)
        finally:
            asyncio.create_subprocess_exec = orig

    handle = asyncio.run(go())
    assert handle.meta["pgid"] == 4242
    _PROCS.pop(handle.job_id, None)
