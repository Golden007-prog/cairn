"""Live NATS verification of the remote-job channel (#311, ADR-0043 S2).

Parity with test_trigger_inbox_nats.py: skips when no broker is reachable at
``OBS_TEST_NATS_URL`` (default ``nats://localhost:4223``; e.g.
``docker run -p 4223:4222 nats -js``). Proves the JetStream channel LIVE:
events-up (heartbeat/succeeded), control-down (abort), last-value retrieval,
and the JobReporter worker loop — the pieces ADR-0043 had only verified
hermetically (InMemory channel).
"""
from __future__ import annotations

import asyncio
import os
import uuid

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

NATS_URL = os.environ.get("OBS_TEST_NATS_URL", "nats://localhost:4223")


def _broker_up() -> bool:
    async def probe() -> bool:
        try:
            import nats

            nc = await nats.connect(NATS_URL, connect_timeout=1)
            await nc.close()
            return True
        except Exception:  # noqa: BLE001
            return False

    try:
        return asyncio.run(probe())
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _broker_up(), reason=f"no NATS broker at {NATS_URL}")


def _channel():
    from cairn.core.operator.job_channel import NatsJobChannel

    # ONE stream (the default) for all tests: JetStream rejects overlapping
    # subject claims across streams (cairn.jobs.> can belong to only one), and
    # per-run subjects + unique run_ids already isolate the tests.
    return NatsJobChannel(NATS_URL)


async def test_events_up_last_value_wins():
    ch = _channel()
    run_id = f"r-{uuid.uuid4().hex[:8]}"
    try:
        assert await ch.latest_event(run_id) is None      # nothing published yet
        await ch.publish_event(run_id, {"state": "running", "metrics": {"loss": 1.2}})
        await ch.publish_event(run_id, {"state": "running", "metrics": {"loss": 0.7}})
        last = await ch.latest_event(run_id)
        assert last["state"] == "running" and last["metrics"]["loss"] == 0.7
        assert last["ts"]                                  # stamped on publish
    finally:
        await ch.aclose()


async def test_control_down_and_run_isolation():
    ch = _channel()
    a, b = f"r-{uuid.uuid4().hex[:8]}", f"r-{uuid.uuid4().hex[:8]}"
    try:
        await ch.publish_control(a, {"action": "abort", "reason": "learning gate"})
        ctl = await ch.latest_control(a)
        assert ctl["action"] == "abort" and ctl["reason"] == "learning gate"
        assert await ch.latest_control(b) is None          # per-run subjects isolate
    finally:
        await ch.aclose()


async def test_job_reporter_heartbeat_abort_loop():
    """The ADR-0043 worker loop, live: heartbeats stream up until the control
    plane publishes an abort; the worker sees it and reports failed."""
    from cairn.core.operator.job_channel import JobReporter

    ch = _channel()
    run_id = f"r-{uuid.uuid4().hex[:8]}"
    rep = JobReporter(ch, run_id)
    try:
        assert await rep.aborted() is False                # no control yet
        await rep.heartbeat({"eval_losses": [1.0, 0.8]})
        assert (await ch.latest_event(run_id))["state"] == "running"

        await ch.publish_control(run_id, {"action": "abort"})   # control-plane kill
        assert await rep.aborted() is True

        await rep.failed("aborted by control")
        last = await ch.latest_event(run_id)
        assert last["state"] == "failed" and "aborted" in last["error"]
    finally:
        await ch.aclose()


async def test_job_reporter_success_path():
    from cairn.core.operator.job_channel import JobReporter

    ch = _channel()
    run_id = f"r-{uuid.uuid4().hex[:8]}"
    rep = JobReporter(ch, run_id)
    try:
        await rep.heartbeat()
        await rep.succeeded({"adapter_uri": "s3://x/adapter"}, {"final_loss": 0.42})
        last = await ch.latest_event(run_id)
        assert last["state"] == "succeeded"
        assert last["output"]["adapter_uri"] == "s3://x/adapter"
        assert last["metrics"]["final_loss"] == 0.42
    finally:
        await ch.aclose()
