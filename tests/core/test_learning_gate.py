"""Mid-train learning gate (#144, ADR-0039 Gate 2): the verdict + the worker abort.

The verdict is a pure function; the worker abort is exercised with injected
poll/resume/cancel fakes (no provider, no GPU).
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from cairn.core.operator.job_control import drive_once
from cairn.core.operator.job_store import FAILED, InMemoryRemoteJobStore, RemoteJobRecord
from cairn.core.operator.jobs import JobStatus
from cairn.core.operator.learning import LearningConfig, learning_verdict


# ── learning_verdict (pure) ──────────────────────────────────────────


def test_warmup_is_learning():
    v = learning_verdict([1.0, 0.9], LearningConfig(warmup=2))
    assert v.learning and "warmup" in v.reason


def test_improving_is_learning():
    v = learning_verdict([1.0, 0.9, 0.8, 0.7], LearningConfig(patience=3, warmup=2))
    assert v.verdict == "learning"


def test_stalled_flat_is_not_learning():
    v = learning_verdict([1.0, 0.5, 0.5, 0.5, 0.5], LearningConfig(patience=3, warmup=2))
    assert v.verdict == "stalled" and not v.learning


def test_diverging_rising_is_not_learning():
    v = learning_verdict([1.0, 0.5, 0.6, 0.7, 0.8], LearningConfig(patience=3, warmup=2))
    assert v.verdict == "diverging" and not v.learning


def test_min_delta_treats_tiny_improvements_as_stalled():
    v = learning_verdict(
        [0.50, 0.499, 0.498, 0.497], LearningConfig(patience=2, warmup=1, min_delta=0.01)
    )
    assert v.verdict == "stalled"  # each drop < min_delta → no real improvement


# ── worker early-abort path ──────────────────────────────────────────


async def _seed(store, run_id="r1"):
    await store.register(
        RemoteJobRecord(run_id=run_id, node_name="train", provider="train/runpod", job_id="j1", backend="runpod")
    )


async def test_running_diverging_aborts_cancels_and_resumes_failed():
    store = InMemoryRemoteJobStore()
    await _seed(store)
    cancelled, resumed = [], []

    async def poll(rec):
        return JobStatus(state="running", metrics={"eval_losses": [1.0, 0.5, 0.6, 0.7, 0.8]})

    async def cancel(rec):
        cancelled.append(rec.run_id)

    async def resume(rec, status):
        resumed.append((rec.run_id, status.state, status.error))

    tally = await drive_once(store, poll, resume, cancel=cancel, learning=LearningConfig(patience=3, warmup=2))
    assert tally["aborted"] == 1 and tally["running"] == 0
    assert cancelled == ["r1"]
    assert resumed and resumed[0][1] == "failed" and "early-abort" in resumed[0][2]
    assert (await store.get("r1:train")).status == FAILED


async def test_running_learning_stays_active_no_abort():
    store = InMemoryRemoteJobStore()
    await _seed(store)
    cancelled = []

    async def poll(rec):
        return JobStatus(state="running", metrics={"eval_losses": [1.0, 0.9, 0.8, 0.7]})

    async def cancel(rec):
        cancelled.append(rec.run_id)

    async def resume(rec, status):
        pass

    tally = await drive_once(store, poll, resume, cancel=cancel, learning=LearningConfig(patience=3, warmup=2))
    assert tally["running"] == 1 and tally["aborted"] == 0
    assert cancelled == []
    assert len(await store.claim_active()) == 1  # still active


async def test_no_learning_config_never_aborts():
    store = InMemoryRemoteJobStore()
    await _seed(store)

    async def poll(rec):
        return JobStatus(state="running", metrics={"eval_losses": [1.0, 0.6, 0.7, 0.8]})

    async def resume(rec, status):
        pass

    # learning/cancel not passed → the metrics are ignored, job stays running.
    tally = await drive_once(store, poll, resume)
    assert tally["running"] == 1 and tally["aborted"] == 0
