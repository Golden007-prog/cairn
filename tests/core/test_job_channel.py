"""Remote-job worker channel (ADR-0043 Slice 2) — events up, control down.

Hermetic: the in-memory channel, no NATS. Covers the channel round-trip, the
channel-backed poll/cancel that drop into the EXISTING control loop, the
worker-side JobReporter, and an end-to-end drive_once where a not-learning loss
trajectory streamed up triggers the learning-gate abort down.
"""
from __future__ import annotations

import pytest

from cairn.core.operator.job_channel import (
    InMemoryJobChannel,
    JobReporter,
    build_channel_cancel,
    build_channel_poll,
    channel_subjects,
)
from cairn.core.operator.job_control import drive_once
from cairn.core.operator.job_store import InMemoryRemoteJobStore, RemoteJobRecord
from cairn.core.operator.learning import LearningConfig

pytestmark = pytest.mark.asyncio


async def test_channel_roundtrip():
    ch = InMemoryJobChannel()
    assert await ch.latest_event("r1") is None
    await ch.publish_event("r1", {"state": "running", "metrics": {"loss": 1.0}})
    ev = await ch.latest_event("r1")
    assert ev["state"] == "running" and ev["metrics"]["loss"] == 1.0 and ev["ts"]
    await ch.publish_control("r1", {"action": "abort"})
    assert (await ch.latest_control("r1"))["action"] == "abort"


async def test_channel_poll_maps_events_to_jobstatus():
    ch = InMemoryJobChannel()
    poll = build_channel_poll(ch)
    rec = RemoteJobRecord(run_id="r1", node_name="train")

    # no event yet ⇒ running
    assert (await poll(rec)).state == "running"

    await ch.publish_event("r1", {"state": "running", "metrics": {"eval_losses": [1.0, 0.9]}})
    st = await poll(rec)
    assert st.state == "running" and st.metrics["eval_losses"] == [1.0, 0.9]

    await ch.publish_event("r1", {"state": "succeeded", "output": {"adapter_uri": "file:///a"}, "metrics": {"train_loss": 0.4}})
    st = await poll(rec)
    assert st.state == "succeeded" and st.output["adapter_uri"] == "file:///a" and st.metrics["train_loss"] == 0.4

    await ch.publish_event("r1", {"state": "failed", "error": "boom"})
    st = await poll(rec)
    assert st.state == "failed" and st.error == "boom"


async def test_channel_cancel_publishes_abort():
    ch = InMemoryJobChannel()
    cancel = build_channel_cancel(ch)
    rec = RemoteJobRecord(run_id="r1")
    await cancel(rec)
    assert (await ch.latest_control("r1"))["action"] == "abort"


async def test_job_reporter_up_and_down():
    ch = InMemoryJobChannel()
    rep = JobReporter(ch, "r1")
    await rep.heartbeat({"eval_losses": [1.0, 0.8]})
    assert (await ch.latest_event("r1"))["state"] == "running"
    assert not await rep.aborted()
    # control pushes an abort down → worker observes it
    await ch.publish_control("r1", {"action": "abort"})
    assert await rep.aborted()
    await rep.succeeded({"adapter_uri": "file:///x"}, {"train_loss": 0.3})
    assert (await ch.latest_event("r1"))["state"] == "succeeded"


async def test_subjects_are_per_run():
    a, b = channel_subjects("run-a"), channel_subjects("run-b")
    assert a["events"] != b["events"] and a["control"] != b["control"]
    assert a["events"].endswith(".events") and a["control"].endswith(".control")


async def test_drive_once_terminal_event_resumes_run():
    """A succeeded event streamed up → drive_once marks done + resumes the run."""
    store = InMemoryRemoteJobStore()
    ch = InMemoryJobChannel()
    rec = await store.register(RemoteJobRecord(run_id="r1", node_name="train", provider="remote/exec"))
    await ch.publish_event("r1", {"state": "succeeded", "output": {"adapter_uri": "file:///a"}})

    resumed: list = []

    async def resume(r, status):
        resumed.append((r.run_id, status.state))

    tally = await drive_once(store, build_channel_poll(ch), resume)
    assert tally["done"] == 1
    assert resumed == [("r1", "succeeded")]
    assert (await store.get(rec.id)).status == "done"


async def test_drive_once_learning_gate_aborts_via_channel():
    """A not-learning eval_losses trajectory streamed up → the learning gate
    aborts: cancel publishes an abort DOWN, the job is failed, the run resumed.
    Proves the channel plugs into the existing gate + loop with no loop changes."""
    store = InMemoryRemoteJobStore()
    ch = InMemoryJobChannel()
    rec = await store.register(RemoteJobRecord(run_id="r1", node_name="train", provider="remote/exec"))
    # diverging loss (mirrors test_learning_gate) streamed up by the worker
    await ch.publish_event("r1", {"state": "running", "metrics": {"eval_losses": [1.0, 0.5, 0.6, 0.7, 0.8]}})

    resumed: list = []

    async def resume(r, status):
        resumed.append(status.state)

    tally = await drive_once(
        store, build_channel_poll(ch), resume,
        cancel=build_channel_cancel(ch), learning=LearningConfig(patience=3, warmup=2),
    )
    assert tally["aborted"] == 1
    assert resumed == ["failed"]
    # the abort was delivered DOWN the control subject for the worker to obey
    assert (await ch.latest_control("r1"))["action"] == "abort"
    assert (await store.get(rec.id)).status == "failed"
