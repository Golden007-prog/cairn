"""Bus Protocol + InProcessBus + dispatcher convergence bridge (ADR-0010 P3.b)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from cairn.core.bus import InProcessBus, get_bus, reset_bus


@pytest.fixture(autouse=True)
def _reset_bus():
    reset_bus()
    yield
    reset_bus()


async def _wait(cond, timeout=1.0):
    async def loop():
        while not cond():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(loop(), timeout)


# ── core pub/sub ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_exact_topic_delivery():
    bus = InProcessBus()
    got = []

    async def handler(m):
        got.append(m)

    await bus.subscribe("incident.rca", handler)
    await bus.publish("incident.rca", {"x": 1}, publisher="incident", correlation_id="run-1")
    await _wait(lambda: got)
    assert got[0].topic == "incident.rca"
    assert got[0].payload == {"x": 1}
    assert got[0].publisher == "incident" and got[0].correlation_id == "run-1"


@pytest.mark.asyncio
async def test_wildcard_prefix_match():
    bus = InProcessBus()
    got = []

    async def handler(m):
        got.append(m.topic)

    await bus.subscribe("incident.*", handler)
    await bus.publish("incident.rca_completed", {})
    await bus.publish("incident.triage.started", {})
    await bus.publish("other.thing", {})       # different namespace → no
    await bus.publish("incident", {})          # no dot → no
    await _wait(lambda: len(got) == 2)
    await asyncio.sleep(0.02)  # let any stray deliveries land
    assert set(got) == {"incident.rca_completed", "incident.triage.started"}


@pytest.mark.asyncio
async def test_star_matches_all_and_multi_pattern():
    bus = InProcessBus()
    a, b = [], []

    async def ha(m):
        a.append(m.topic)

    async def hb(m):
        b.append(m.topic)

    await bus.subscribe("*", ha)
    await bus.subscribe(["x.one", "y.two"], hb)  # list of patterns
    await bus.publish("x.one", {})
    await bus.publish("z.other", {})
    await _wait(lambda: len(a) == 2 and len(b) == 1)
    assert set(a) == {"x.one", "z.other"}
    assert b == ["x.one"]


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery():
    bus = InProcessBus()
    got = []

    async def handler(m):
        got.append(m)

    sub = await bus.subscribe("t", handler)
    await bus.publish("t", {})
    await _wait(lambda: len(got) == 1)
    sub.unsubscribe()
    await bus.publish("t", {})
    await asyncio.sleep(0.03)
    assert len(got) == 1  # nothing after unsubscribe


@pytest.mark.asyncio
async def test_handler_raise_is_isolated_and_at_most_once():
    bus = InProcessBus()
    got = []

    async def boom(m):
        raise RuntimeError("handler down")

    async def ok(m):
        got.append(m.topic)

    await bus.subscribe("t", boom)
    await bus.subscribe("t", ok)
    await bus.publish("t", {})          # boom drops, ok still delivers
    await _wait(lambda: len(got) == 1)
    await bus.publish("t", {})          # bus survives a prior raise
    await _wait(lambda: len(got) == 2)


@pytest.mark.asyncio
async def test_replay_unsupported_on_inprocess():
    bus = InProcessBus()
    agen = bus.replay("t", datetime(2026, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(NotImplementedError):
        await agen.__anext__()


# ── factory ────────────────────────────────────────────────────────────

def test_get_bus_default_is_inprocess(monkeypatch):
    monkeypatch.delenv("OBS_BUS", raising=False)
    reset_bus()
    assert isinstance(get_bus(), InProcessBus)


def test_get_bus_postgres_returns_postgresbus(monkeypatch):
    # #477 — postgres:// is now the Tier-2 PostgresBus (was a fall-back stub).
    from cairn.core.bus.postgres import PostgresBus

    monkeypatch.setenv("OBS_BUS", "postgres://x")
    reset_bus()
    assert isinstance(get_bus(), PostgresBus)


def test_get_bus_redis_is_refused(monkeypatch):
    # ADR-0127 D5 — the redis:// branch that only logged "not implemented" and
    # degraded to InProcessBus is gone: selecting it fails closed, naming the
    # two supported backends, and caches nothing.
    monkeypatch.setenv("OBS_BUS", "redis://x")
    reset_bus()
    with pytest.raises(ValueError, match=r"inprocess.*postgres://") as ei:
        get_bus()
    assert "redis://" in str(ei.value)
    monkeypatch.delenv("OBS_BUS")
    assert isinstance(get_bus(), InProcessBus)  # the refusal cached nothing
    reset_bus()


@pytest.mark.parametrize("raw, backend", [
    ("redis://x", "refused"), ("rediss://x", "refused"), ("REDIS://x", "refused"),
    ("postgres://x", "postgres"), ("postgresql://x", "postgres"),
    ("", "inprocess"), ("inprocess", "inprocess"), ("nats://x", "unrecognized"),
])
def test_bus_backend_reads_the_scheme(raw, backend):
    # ADR-0127 D5 — redis and rediss are both refused; other unknowns keep the
    # warn-and-fall-back path (get_bus runs in-process for "unrecognized").
    from cairn.core.bus import bus_backend
    assert bus_backend(raw) == backend


def test_get_bus_rediss_is_refused(monkeypatch):
    monkeypatch.setenv("OBS_BUS", "rediss://x")
    reset_bus()
    with pytest.raises(ValueError, match="postgres://"):
        get_bus()
    reset_bus()


# ── dispatcher convergence bridge ──────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatcher_emit_bridges_run_events_to_bus():
    from cairn.core.dispatch.dispatcher import RunDispatcher
    from cairn.core.storage import InMemoryRunStore

    got = []

    async def handler(m):
        got.append(m)

    await get_bus().subscribe("run.*", handler)
    disp = RunDispatcher(runner=object(), store=InMemoryRunStore())
    # _emit publishes to the bus even if the append leg no-ops (best-effort).
    await disp._emit("run-1", "findings", {"n": 3})
    await _wait(lambda: got)
    assert got[0].topic == "run.findings"
    assert got[0].correlation_id == "run-1"
    assert got[0].payload == {"n": 3}
