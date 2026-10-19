"""Bus → skill activation: subscribes_to matching + BusSkillBridge (ADR-0010 P3.c)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cairn.core.bus import InProcessBus, reset_bus
from cairn.core.dispatch.bus_bridge import BusSkillBridge
from cairn.core.engine.schema import TriggerEvent, TriggersSpec
from cairn.core.engine.skill_engine import match_skill_to_event


def _skill(subscribes_to):
    return SimpleNamespace(triggers=TriggersSpec(subscribes_to=subscribes_to))


def _runner(skills):
    return SimpleNamespace(skill_engine=SimpleNamespace(skills=skills))


class _FakeDispatcher:
    def __init__(self):
        self.submitted = []

    async def submit(self, event, lane="realtime", **kw):
        self.submitted.append((event, lane))
        return SimpleNamespace(id="inv-x")


async def _wait(cond, timeout=1.0):
    async def loop():
        while not cond():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(loop(), timeout)


@pytest.fixture(autouse=True)
def _reset_bus():
    reset_bus()
    yield
    reset_bus()


# ── match_skill_to_event (source="event") ──────────────────────────────

def test_event_source_matches_wildcard_topic():
    s = _skill(["incident.*"])
    assert match_skill_to_event(s, TriggerEvent(source="event", matched_pattern="incident.rca_completed"))
    assert not match_skill_to_event(s, TriggerEvent(source="event", matched_pattern="other.thing"))


def test_event_source_exact_topic():
    s = _skill(["incident.rca_completed"])
    assert match_skill_to_event(s, TriggerEvent(source="event", matched_pattern="incident.rca_completed"))
    assert not match_skill_to_event(s, TriggerEvent(source="event", matched_pattern="incident.triage"))


def test_event_no_match_when_unsubscribed():
    s = _skill([])
    assert not match_skill_to_event(s, TriggerEvent(source="event", matched_pattern="incident.x"))


# ── BusSkillBridge ──────────────────────────────────────────────────────

def test_bridge_unions_subscribed_topics():
    r = _runner([_skill(["incident.*"]), _skill(["a.b", "incident.*"])])
    b = BusSkillBridge(r, _FakeDispatcher(), bus=InProcessBus())
    assert b.subscribed_topics() == ["a.b", "incident.*"]


@pytest.mark.asyncio
async def test_bridge_fires_on_matching_domain_event():
    bus = InProcessBus()
    disp = _FakeDispatcher()
    bridge = BusSkillBridge(_runner([_skill(["incident.*"])]), disp, bus=bus)
    assert await bridge.start() == 1

    await bus.publish("incident.rca_completed", {"sev": "high"}, correlation_id="run-9")
    await _wait(lambda: disp.submitted)
    event, lane = disp.submitted[0]
    assert event.source == "event"
    assert event.matched_pattern == "incident.rca_completed"
    assert event.payload == {"sev": "high"}
    assert lane == "background"


@pytest.mark.asyncio
async def test_bridge_ignores_run_firehose_loop_guard():
    bus = InProcessBus()
    disp = _FakeDispatcher()
    # Even if a skill explicitly subscribes to run.*, the engine firehose is
    # reserved — the bridge drops it (prevents self-trigger loops).
    bridge = BusSkillBridge(_runner([_skill(["run.*"])]), disp, bus=bus)
    await bridge.start()
    await bus.publish("run.done", {})
    await asyncio.sleep(0.03)
    assert disp.submitted == []


@pytest.mark.asyncio
async def test_bridge_inert_without_subscriptions():
    bridge = BusSkillBridge(_runner([_skill([])]), _FakeDispatcher(), bus=InProcessBus())
    assert await bridge.start() == 0


@pytest.mark.asyncio
async def test_bridge_stop_detaches():
    bus = InProcessBus()
    disp = _FakeDispatcher()
    bridge = BusSkillBridge(_runner([_skill(["incident.*"])]), disp, bus=bus)
    await bridge.start()
    bridge.stop()
    await bus.publish("incident.rca_completed", {})
    await asyncio.sleep(0.03)
    assert disp.submitted == []
