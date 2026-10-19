"""#2025 (ADR-0102 D4) — a worker's report events reach the store ONCE each, over
either transport, whatever the network did; control goes the other way through
the same log.

The acceptance the ticket names: cut the network for a window during a
200-step run; after reconnect the store has exactly 200 ``metrics`` events with
contiguous ``seq`` and no duplicates. Here the "network" is a transport whose
batches fail for a window and are replayed; the ingest must make it exact.
"""
from __future__ import annotations

import asyncio

import pytest

from cairn.core.operator import report_ingest as ri
from cairn.core.operator.job_channel import (
    InMemoryJobChannel,
    StoreJobChannel,
    drain_channel_events,
    get_job_channel,
)
from cairn.core.operator.job_control import build_default_drain, drive_once
from cairn.core.operator.job_store import InMemoryRemoteJobStore, RemoteJobRecord
from cairn.core.operator.jobs import JobStatus
from cairn.core.storage.memory.runs import InMemoryRunStore

pytestmark = pytest.mark.asyncio


@pytest.fixture
def store():
    ri._HWM.clear()
    return InMemoryRunStore()


def _metrics(node: str, seq: int, step: int) -> dict:
    return {"type": "metrics", "data": {"node": node, "seq": seq, "step": step, "series": {"loss": 1.0 / step}}}


# ─── 1. idempotent ingest ─────────────────────────────────────────────────


async def test_replayed_batches_land_once_with_contiguous_seq(store):
    """A flaky link: every batch is sent, some fail and are re-sent whole, one
    batch arrives twice. The store ends with exactly 200 metrics, seq 1..200."""
    run = "run-t6"
    steps = 200
    batches = [[_metrics("train", s, s) for s in range(i, min(i + 25, steps + 1))] for i in range(1, steps + 1, 25)]
    # the network cut: batches 3..4 "fail" (never reach us) then the worker replays from batch 3
    delivered = batches[:2] + batches[2:] + [batches[5]]  # + one straight duplicate
    total_accepted = 0
    for b in delivered:
        res = await ri.ingest_events(store, run, b)
        total_accepted += res.accepted
    events = [e for e in await store.get_events(run) if e.type == "metrics"]
    assert total_accepted == steps
    assert len(events) == steps
    assert [e.data["seq"] for e in events] == list(range(1, steps + 1))
    assert len({e.data["seq"] for e in events}) == steps


async def test_ack_carries_the_high_water_mark_per_node(store):
    res = await ri.ingest_events(store, "r", [_metrics("train", 1, 1), _metrics("train", 2, 2),
                                             {"type": "progress", "data": {"node": "eval", "seq": 1, "stage": "eval"}}])
    assert res.accepted == 3 and res.dropped == 0
    assert res.last_seq == {"train": 2, "eval": 1}
    res = await ri.ingest_events(store, "r", [_metrics("train", 2, 2), _metrics("train", 3, 3)])
    assert res.accepted == 1 and res.dropped == 1 and res.last_seq["train"] == 3


async def test_out_of_order_within_a_batch_is_applied_in_seq_order(store):
    await ri.ingest_events(store, "r", [_metrics("n", 3, 3), _metrics("n", 1, 1), _metrics("n", 2, 2)])
    assert [e.data["seq"] for e in await store.get_events("r")] == [1, 2, 3]


async def test_non_report_types_and_missing_seq_are_refused_by_name(store):
    res = await ri.ingest_events(store, "r", [
        {"type": "done", "data": {"node": "n", "seq": 1}},          # lifecycle: the executor's
        {"type": "parameters", "data": {"node": "n", "seq": 1}},    # the runtime's
        {"type": "metrics", "data": {"node": "n", "step": 1}},      # no seq
        _metrics("n", 1, 1),
    ])
    assert res.accepted == 1
    assert len(res.refused) == 3 and "done" in res.refused[0] and "parameters" in res.refused[1] and "seq" in res.refused[2]
    assert await store.get_events("r") and len(await store.get_events("r")) == 1


async def test_high_water_mark_is_seeded_from_the_store_after_a_restart(store):
    """A control-plane restart forgets the in-process mark; the store remembers."""
    await ri.ingest_events(store, "r", [_metrics("n", 1, 1), _metrics("n", 2, 2)])
    ri.forget_run("r")
    res = await ri.ingest_events(store, "r", [_metrics("n", 1, 1), _metrics("n", 2, 2), _metrics("n", 3, 3)])
    assert res.accepted == 1 and res.dropped == 2
    assert len(await store.get_events("r")) == 3


# ─── 2. control, the other direction ──────────────────────────────────────


async def test_control_is_a_run_event_and_the_newest_one_wins(store):
    assert await ri.latest_control(store, "r") is None
    a = await ri.record_control(store, "r", "abort", by="gate")
    b = await ri.record_control(store, "r", "checkpoint-and-stop", node="train", by="console")
    latest = await ri.latest_control(store, "r")
    assert latest["action"] == "checkpoint-and-stop" and latest["seq"] == b["seq"] > a["seq"]
    assert await ri.latest_control(store, "r", after_seq=b["seq"]) is None


async def test_unknown_control_action_is_refused():
    with pytest.raises(ValueError, match="abort"):
        await ri.record_control(InMemoryRunStore(), "r", "pause")


async def test_wait_control_returns_when_a_request_arrives(store):
    async def later():
        await asyncio.sleep(0.05)
        await ri.record_control(store, "r", "abort")

    asyncio.get_running_loop().create_task(later())
    got = await ri.wait_control(store, "r", timeout_s=2.0)
    assert got and got["action"] == "abort"


async def test_wait_control_times_out_to_none(store):
    assert await ri.wait_control(store, "r", timeout_s=0.05) is None


# ─── 3. the two channels feed one log ─────────────────────────────────────


async def test_store_channel_is_the_http_backend(monkeypatch, store):
    monkeypatch.setenv("OBS_JOB_CHANNEL", "http")
    assert isinstance(get_job_channel(), StoreJobChannel)
    chan = StoreJobChannel(store)
    await chan.publish_event("r", _metrics("n", 1, 1))
    await chan.publish_event("r", _metrics("n", 1, 1))   # replay
    await chan.publish_event("r", {"state": "running", "metrics": {"eval_losses": [1.0]}})  # JobReporter heartbeat
    assert [e.type for e in await store.get_events("r")] == ["metrics", "job"]
    assert (await chan.latest_event("r"))["state"] == "running"
    await chan.publish_control("r", {"action": "abort"})
    assert (await chan.latest_control("r"))["action"] == "abort"
    assert await chan.events_since("r") == ([], None)


async def test_in_memory_channel_drains_into_the_store_once(store):
    chan = InMemoryJobChannel()
    for s in (1, 2, 3):
        await chan.publish_event("r", _metrics("train", s, s))
    n, cur = await drain_channel_events(chan, store, "r")
    assert n == 3
    await chan.publish_event("r", _metrics("train", 4, 4))
    n, cur = await drain_channel_events(chan, store, "r", cur)
    assert n == 1
    n, _ = await drain_channel_events(chan, store, "r", None)  # a restarted drain replays from the start
    assert n == 0
    assert [e.data["seq"] for e in await store.get_events("r")] == [1, 2, 3, 4]


async def test_drive_once_drains_before_polling(store):
    """The control loop moves the worker's events into the store on every tick,
    so a job that finishes this tick has its whole curve recorded before the
    run resumes."""
    chan = InMemoryJobChannel()
    jobs = InMemoryRemoteJobStore()
    await jobs.register(RemoteJobRecord(run_id="run-1", node_name="train", provider="train/modal", job_id="j1"))
    for s in (1, 2):
        await chan.publish_event("run-1", _metrics("train", s, s))
    order: list[str] = []

    async def poll(rec):
        order.append("poll")
        return JobStatus(state="succeeded", output={"ok": True})

    async def resume(rec, status):
        order.append("resume")

    drain = build_default_drain(chan, store)

    async def counting_drain(rec):
        order.append("drain")
        return await drain(rec)

    tally = await drive_once(jobs, poll, resume, drain=counting_drain)
    assert tally["done"] == 1
    assert order == ["drain", "poll", "resume"]
    assert [e.data["seq"] for e in await store.get_events("run-1") if e.type == "metrics"] == [1, 2]


async def test_a_drain_that_raises_does_not_kill_the_loop(store):
    jobs = InMemoryRemoteJobStore()
    await jobs.register(RemoteJobRecord(run_id="r", node_name="n", provider="p", job_id="j"))

    async def bad_drain(rec):
        raise RuntimeError("broker gone")

    async def poll(rec):
        return JobStatus(state="running")

    async def resume(rec, status):
        pass

    tally = await drive_once(jobs, poll, resume, drain=bad_drain)
    assert tally["running"] == 1
