"""#127 / ADR-0038 — the durable remote-job registry (provisioning seam).

The operator registers a job + the run pauses; a control worker claims active
jobs, drives them, and on terminal marks the result for resume. Mirrors the
trigger-inbox tests.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from cairn.core.operator.job_store import (
    DONE,
    RUNNING,
    InMemoryRemoteJobStore,
    RemoteJobRecord,
    SqliteRemoteJobStore,
    get_remote_job_store,
)
from cairn.core.operator.jobs import JobHandle


def _rec(run_id="run-1", node="train", **kw):
    return RemoteJobRecord(run_id=run_id, node_name=node, provider="runpod", job_id="rp-job-1", backend="remote_job", **kw)


async def test_id_defaults_to_run_and_node():
    rec = _rec()
    assert rec.id == "run-1:train"
    # handle reconstructs for re-poll
    h = rec.handle()
    assert isinstance(h, JobHandle) and h.job_id == "rp-job-1" and h.backend == "remote_job"


async def test_register_claim_then_resume_lifecycle():
    store = InMemoryRemoteJobStore()
    await store.register(_rec(compute_target="my-runpod"))
    # worker claims the active job
    active = await store.claim_active()
    assert [r.id for r in active] == ["run-1:train"]
    assert active[0].compute_target == "my-runpod"
    # still active while running
    await store.mark_running("run-1:train")
    assert len(await store.claim_active()) == 1
    # terminal → result recorded, no longer claimed
    await store.mark_done("run-1:train", {"adapter_uri": "s3://b/adapter"})
    assert await store.claim_active() == []
    done = await store.get("run-1:train")
    assert done.status == DONE and done.result == {"adapter_uri": "s3://b/adapter"}


async def test_failed_and_cancelled_leave_active():
    store = InMemoryRemoteJobStore()
    await store.register(_rec(run_id="r2"))
    await store.register(_rec(run_id="r3"))
    await store.mark_failed("r2:train", "boom")
    await store.mark_cancelled("r3:train")
    assert await store.claim_active() == []
    assert (await store.get("r2:train")).error == "boom"


async def test_sqlite_persists_active_job_across_reopen(tmp_path):
    db = str(tmp_path / "jobs.db")
    a = SqliteRemoteJobStore(db)
    await a.register(_rec(compute_target="my-runpod"))
    await a.mark_running("run-1:train")
    a.close()
    # a restarted worker re-attaches to the still-active job by JobHandle
    b = SqliteRemoteJobStore(db)
    active = await b.claim_active()
    assert [r.id for r in active] == ["run-1:train"] and active[0].status == RUNNING
    assert active[0].handle().job_id == "rp-job-1"
    await b.mark_done("run-1:train", {"adapter_uri": "s3://b/a"})
    assert await b.claim_active() == []
    b.close()


def test_factory_memory_selector(monkeypatch):
    monkeypatch.setenv("OBS_JOB_STORE", "memory")
    assert isinstance(get_remote_job_store(), InMemoryRemoteJobStore)
