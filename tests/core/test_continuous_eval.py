"""Continuous-eval consumer (#477) — hermetic tests over InProcessBus.

The consumer subscribes to the run-completion firehose (`run.done`/`run.failed`)
and re-fires each run as the matching `cairn.run_hooks` event (`run.finished` /
`run.failed`, #2421 decision 7) with `source="continuous_eval"` — so whoever
scores a run on the dispatcher's terminal path scores it here too. The scoring
itself (and its idempotency) is the platform's:
packages/cairn-platform/tests/test_continuous_eval_scoring.py.
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.bus import InProcessBus
from cairn.core.eval.continuous import (
    ContinuousEvalConsumer,
    continuous_eval_enabled,
    maybe_start_continuous_eval,
)
from cairn.core.pack import run_hooks
from cairn.core.storage import InMemoryRunStore


@pytest.fixture()
def heard(monkeypatch):
    got: list[tuple[str, dict]] = []

    async def probe(event, payload):
        got.append((event, dict(payload)))

    monkeypatch.setattr(run_hooks, "load_run_hooks", lambda: {"probe": probe})
    return got


async def _wait(heard, n: int, tries: int = 100) -> list:
    for _ in range(tries):
        if len(heard) >= n:
            break
        await asyncio.sleep(0.01)
    return heard


# ─── the consumer fires the run hooks on the completion firehose ───────


@pytest.mark.asyncio
async def test_consumer_fires_run_finished_on_run_done(heard):
    store = InMemoryRunStore()
    bus = InProcessBus()
    consumer = await ContinuousEvalConsumer(store).start(bus)
    try:
        await bus.publish("run.done", {}, correlation_id="run-c1")
        got = await _wait(heard, 1)
        assert got == [("run.finished", {"event": "run.finished", "run_id": "run-c1", "status": "done",
                                         "store": store, "source": "continuous_eval"})]
    finally:
        consumer.stop()


@pytest.mark.asyncio
async def test_consumer_fires_run_failed_on_run_failed_and_reads_a_payload_run_id(heard):
    store = InMemoryRunStore()
    bus = InProcessBus()
    consumer = await ContinuousEvalConsumer(store).start(bus)
    try:
        await bus.publish("run.failed", {"run_id": "run-c2"})
        got = await _wait(heard, 1)
        assert [(e, p["run_id"], p["status"], p["source"]) for e, p in got] == [
            ("run.failed", "run-c2", "failed", "continuous_eval")]
    finally:
        consumer.stop()


@pytest.mark.asyncio
async def test_consumer_ignores_message_without_run_id(heard):
    bus = InProcessBus()
    consumer = await ContinuousEvalConsumer(InMemoryRunStore()).start(bus)
    try:
        await bus.publish("run.done", {})  # no correlation_id, no payload run_id
        await asyncio.sleep(0.05)
        assert heard == []
    finally:
        consumer.stop()


@pytest.mark.asyncio
async def test_with_no_listener_installed_nothing_happens(monkeypatch):
    """The public core: the consumer runs, the event finds no listener, no error."""
    monkeypatch.setattr(run_hooks, "load_run_hooks", lambda: {})
    bus = InProcessBus()
    consumer = await ContinuousEvalConsumer(InMemoryRunStore()).start(bus)
    try:
        await bus.publish("run.done", {}, correlation_id="run-c3")
        await asyncio.sleep(0.05)
    finally:
        consumer.stop()


# ─── opt-in gate ───────────────────────────────────────────────────────


def test_continuous_eval_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OBS_CONTINUOUS_EVAL", raising=False)
    assert continuous_eval_enabled() is False


@pytest.mark.parametrize("val", ["on", "1", "true", "YES"])
def test_continuous_eval_enabled_values(monkeypatch, val):
    monkeypatch.setenv("OBS_CONTINUOUS_EVAL", val)
    assert continuous_eval_enabled() is True


@pytest.mark.asyncio
async def test_maybe_start_returns_none_when_disabled(monkeypatch):
    monkeypatch.delenv("OBS_CONTINUOUS_EVAL", raising=False)
    assert await maybe_start_continuous_eval(InProcessBus(), InMemoryRunStore()) is None


@pytest.mark.asyncio
async def test_maybe_start_returns_consumer_when_enabled(monkeypatch):
    monkeypatch.setenv("OBS_CONTINUOUS_EVAL", "on")
    got = await maybe_start_continuous_eval(InProcessBus(), InMemoryRunStore())
    assert isinstance(got, ContinuousEvalConsumer)
    got.stop()
