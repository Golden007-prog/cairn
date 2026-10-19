"""Tests for streaming partial outputs (engine capability #90).

Three layers, all deterministic / offline (no LLM, no live backend):

  1. RunStore append-only event log — append assigns monotonic seq, get_events
     filters by after_seq, for BOTH the in-memory and SQLite stores.
  2. subscribe() live semantics — a subscriber registered mid-flight receives
     the backlog then the live tail without gaps or duplicates, and stops at a
     terminal event.
  3. End-to-end through the dispatcher — a FakeRunner that drives the event
     sink produces a stream a follower can read while/after the run executes,
     plus the SSE HTTP surface.
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.dispatch.dispatcher import RunDispatcher
from cairn.core.storage import (
    InMemoryRunStore,
    Run,
    RunEvent,
    SqliteRunStore,
    TERMINAL_EVENT_TYPES,
)
from cairn.core.engine.schema import TriggerEvent


def _event(service="payment-service", pattern="pool exhausted", source="pattern"):
    return TriggerEvent(source=source, matched_pattern=pattern, service=service)


def _run(run_id="run-1"):
    return Run(id=run_id, trigger=_event(), fingerprint="fp")


# ─── Layer 1: append-only log on both stores ─────────────────────────


@pytest.mark.parametrize("store_factory", [InMemoryRunStore, SqliteRunStore])
async def test_append_assigns_monotonic_seq(store_factory):
    store = store_factory() if store_factory is InMemoryRunStore else store_factory(":memory:")
    await store.create(_run())

    e1 = await store.append_event("run-1", "running", {})
    e2 = await store.append_event("run-1", "findings", {"k": "v"})
    e3 = await store.append_event("run-1", "done", {"matched_skills": 1})

    assert (e1.seq, e2.seq, e3.seq) == (1, 2, 3)
    assert e2.data == {"k": "v"}

    # Per-run seq counters are independent.
    other = await store.append_event("run-2", "running", {})
    assert other.seq == 1

    if hasattr(store, "close"):
        store.close()


@pytest.mark.parametrize("store_factory", [InMemoryRunStore, SqliteRunStore])
async def test_get_events_filters_after_seq(store_factory):
    store = store_factory() if store_factory is InMemoryRunStore else store_factory(":memory:")
    for t in ("queued", "running", "findings", "done"):
        await store.append_event("run-1", t, {})

    assert [e.seq for e in await store.get_events("run-1")] == [1, 2, 3, 4]
    assert [e.type for e in await store.get_events("run-1", after_seq=2)] == [
        "findings",
        "done",
    ]
    assert await store.get_events("run-1", after_seq=4) == []
    assert await store.get_events("missing") == []

    if hasattr(store, "close"):
        store.close()


def test_sqlite_events_persist_across_reopen(tmp_path):
    path = str(tmp_path / "runs.db")

    async def _write():
        s = SqliteRunStore(path)
        await s.append_event("run-1", "running", {})
        await s.append_event("run-1", "done", {"matched_skills": 2})
        s.close()

    async def _read():
        s = SqliteRunStore(path)
        evs = await s.get_events("run-1")
        s.close()
        return evs

    asyncio.run(_write())
    evs = asyncio.run(_read())
    assert [e.type for e in evs] == ["running", "done"]
    assert evs[1].data == {"matched_skills": 2}


# ─── Layer 2: subscribe() live semantics ─────────────────────────────


async def test_subscribe_replays_backlog_then_stops_at_terminal():
    store = InMemoryRunStore()
    for t in ("queued", "running", "findings", "done"):
        await store.append_event("run-1", t, {})

    seen = [ev.type async for ev in store.subscribe("run-1")]
    # Full backlog replayed; stream closes at the terminal "done".
    assert seen == ["queued", "running", "findings", "done"]


async def test_subscribe_receives_live_events_without_gap_or_dup():
    store = InMemoryRunStore()
    await store.append_event("run-1", "queued", {})  # 1 event of backlog

    received: list[RunEvent] = []
    ready = asyncio.Event()

    async def consume():
        async for ev in store.subscribe("run-1"):
            received.append(ev)
            if ev.seq == 1:
                ready.set()  # backlog drained; now live

    task = asyncio.create_task(consume())
    await ready.wait()  # ensure subscriber is past backlog and tailing live

    await store.append_event("run-1", "running", {})
    await store.append_event("run-1", "findings", {})
    await store.append_event("run-1", "done", {})

    await asyncio.wait_for(task, timeout=2.0)

    seqs = [e.seq for e in received]
    assert seqs == [1, 2, 3, 4]  # no gaps, no duplicates
    assert received[-1].type == "done"


async def test_after_seq_resumes_a_dropped_stream():
    store = InMemoryRunStore()
    for t in ("queued", "running", "findings", "done"):
        await store.append_event("run-1", t, {})

    # Client that already saw seq<=2 reconnects with after_seq=2.
    seen = [ev.seq async for ev in store.subscribe("run-1", after_seq=2)]
    assert seen == [3, 4]


def test_terminal_event_types():
    """Every way a run can STOP must close the stream, or the console shows a
    finished run as still live.

    `expired` was added to the engine (an approval that timed out ends the run)
    and this assertion was not updated, so it has been red ever since — the test
    was pinning a set the engine had outgrown, not a contract the engine broke.
    """
    assert TERMINAL_EVENT_TYPES == frozenset({"done", "failed", "cancelled", "expired"})


# ─── Layer 3: end-to-end through the dispatcher ──────────────────────


class _StreamingRunner:
    """Runner stand-in that drives the event_sink like the real Runner does."""

    def __init__(self, *, gate: asyncio.Event | None = None):
        self.gate = gate

    async def run(self, event, *, environment=None, event_sink=None, run_id=None, _cascade_depth=0):
        if event_sink:
            await event_sink("skill_started", {"skill_name": "db-pool"})
        if self.gate is not None:
            await self.gate.wait()
        if event_sink:
            await event_sink("findings", {"skill_name": "db-pool", "findings": {"root": "x"}})
            await event_sink("skill_completed", {"skill_name": "db-pool"})
        import types

        return [
            types.SimpleNamespace(
                skill_name="db-pool", template_name="rca",
                duration_seconds=0.0, actions=[],
            )
        ]


async def test_dispatcher_streams_full_lifecycle():
    d = RunDispatcher(_StreamingRunner())
    inv = await d.submit(_event(), wait=True)

    evs = await d.events(inv.id)
    types_ = [e.type for e in evs]
    # Dispatcher lifecycle wraps the runner-emitted events.
    assert types_[0] == "queued"
    assert "running" in types_
    assert "skill_started" in types_
    assert "findings" in types_
    assert "skill_completed" in types_
    assert types_[-1] == "done"
    # seqs are contiguous and monotonic.
    assert [e.seq for e in evs] == list(range(1, len(evs) + 1))


async def test_follower_streams_while_run_in_flight():
    """A follower subscribes to a still-running run and sees events arrive
    before the run completes — proving we don't block until done."""
    gate = asyncio.Event()
    d = RunDispatcher(_StreamingRunner(gate=gate))

    inv = await d.submit(_event(), wait=False)  # fire-and-forget; still running

    collected: list[str] = []
    saw_findings = asyncio.Event()

    async def follow():
        async for ev in d.subscribe(inv.id):
            collected.append(ev.type)
            if ev.type == "findings":
                saw_findings.set()

    follower = asyncio.create_task(follow())

    # Let the run proceed past the gate so it emits findings + completes.
    gate.set()
    await asyncio.wait_for(saw_findings.wait(), timeout=2.0)
    await asyncio.wait_for(follower, timeout=2.0)

    assert "findings" in collected
    assert collected[-1] == "done"
    await d.aclose()


async def test_failed_run_emits_terminal_failed_event():
    class _Boom:
        async def run(self, event, *, environment=None, event_sink=None, run_id=None, _cascade_depth=0):
            if event_sink:
                await event_sink("skill_started", {"skill_name": "x"})
            raise RuntimeError("kaboom")

    d = RunDispatcher(_Boom())
    inv = await d.submit(_event(), wait=True)
    evs = await d.events(inv.id)
    types_ = [e.type for e in evs]
    assert types_[-1] == "failed"
    assert evs[-1].data["error"] == "kaboom"


# ─── SSE HTTP surface ────────────────────────────────────────────────


async def test_sse_endpoint_streams_events():
    from fastapi.testclient import TestClient

    from cairn.core.triggers.server import build_app

    runner = _StreamingRunner()
    app = build_app(runner, enable_scheduler=False)

    with TestClient(app) as client:
        # Kick off a run via the manual webhook (wait=True → finishes).
        resp = client.post("/triggers/manual", json={"skill_name": "db-pool"})
        assert resp.status_code == 200
        run_id = resp.json()["run_id"]

        # Non-streaming snapshot.
        snap = client.get(f"/runs/{run_id}/events")
        assert snap.status_code == 200
        body = snap.json()
        assert body["count"] >= 1
        assert body["events"][-1]["type"] == "done"

        # SSE stream replays the backlog of the finished run.
        with client.stream("GET", f"/runs/{run_id}/stream") as s:
            assert s.status_code == 200
            assert "text/event-stream" in s.headers["content-type"]
            text = "".join(s.iter_text())
        assert "event: done" in text
        assert "event: findings" in text

        # 404 for unknown run.
        assert client.get("/runs/nope/events").status_code == 404


async def test_streaming_runner_requires_skill_match_for_real_app():
    """The manual webhook builds a TriggerEvent; the fake runner ignores
    matching and always returns one result, so run_id is always present."""
    from fastapi.testclient import TestClient

    from cairn.core.triggers.server import build_app

    app = build_app(_StreamingRunner(), enable_scheduler=False)
    with TestClient(app) as client:
        resp = client.post("/triggers/manual", json={})
        assert resp.status_code == 200
        assert resp.json()["run_id"]


async def test_last_event_id_resumes_a_reconnected_stream():
    """#1574 — a browser resuming an SSE stream must not be sent the backlog again.

    Each frame carries ``id: <seq>``, so EventSource replays that id back in the
    ``Last-Event-ID`` header when it reconnects on its own. The endpoint read only
    ``?after_seq=``, which EventSource cannot set on its own retry — so every
    reconnect replayed the whole run, and the console's event trail (the run's
    evidence) grew a duplicate copy of itself each time.
    """
    from fastapi.testclient import TestClient

    from cairn.core.triggers.server import build_app

    app = build_app(_StreamingRunner(), enable_scheduler=False)
    with TestClient(app) as client:
        run_id = client.post("/triggers/manual", json={"skill_name": "db-pool"}).json()["run_id"]

        with client.stream("GET", f"/runs/{run_id}/stream") as s:
            first = "".join(s.iter_text())
        ids = [int(line.split(": ", 1)[1]) for line in first.splitlines() if line.startswith("id: ")]
        assert len(ids) >= 2, f"need a multi-event run to test resumption, got {ids}"

        # Reconnect the way a browser does: the last id it saw, in the header.
        with client.stream("GET", f"/runs/{run_id}/stream",
                           headers={"Last-Event-ID": str(ids[-1])}) as s:
            resumed = "".join(s.iter_text())
        assert [int(x.split(": ", 1)[1]) for x in resumed.splitlines() if x.startswith("id: ")] == [], \
            "reconnect replayed events the client had already seen"

        # Mid-stream resume returns only what came after.
        with client.stream("GET", f"/runs/{run_id}/stream",
                           headers={"Last-Event-ID": str(ids[0])}) as s:
            partial = "".join(s.iter_text())
        assert [int(x.split(": ", 1)[1]) for x in partial.splitlines() if x.startswith("id: ")] == ids[1:]


async def test_an_explicit_after_seq_wins_over_the_header():
    """A caller that names a cursor means it — the header is only the fallback
    for EventSource, which cannot set a query string on its own retry."""
    from fastapi.testclient import TestClient

    from cairn.core.triggers.server import build_app

    app = build_app(_StreamingRunner(), enable_scheduler=False)
    with TestClient(app) as client:
        run_id = client.post("/triggers/manual", json={"skill_name": "db-pool"}).json()["run_id"]
        with client.stream("GET", f"/runs/{run_id}/stream?after_seq=0",
                           headers={"Last-Event-ID": "999"}) as s:
            text = "".join(s.iter_text())
        assert "event: done" in text, "an explicit after_seq=0 must still replay the backlog"


async def test_a_malformed_last_event_id_falls_back_to_the_backlog():
    """A junk header costs a replay — the pre-#1574 behaviour — rather than
    breaking the reconnect with a 400."""
    from fastapi.testclient import TestClient

    from cairn.core.triggers.server import build_app

    app = build_app(_StreamingRunner(), enable_scheduler=False)
    with TestClient(app) as client:
        run_id = client.post("/triggers/manual", json={"skill_name": "db-pool"}).json()["run_id"]
        with client.stream("GET", f"/runs/{run_id}/stream",
                           headers={"Last-Event-ID": "not-a-number"}) as s:
            text = "".join(s.iter_text())
        assert "event: done" in text


def _frame_ids(text: str) -> list[int]:
    """The ``id:`` line of every SSE frame in ``text`` — the seq a browser
    replays back to us in ``Last-Event-ID``."""
    return [int(x.split(": ", 1)[1]) for x in text.splitlines() if x.startswith("id: ")]


async def test_last_event_id_resumes_through_the_workspace_scoped_path():
    """#1387 — the resume must survive the URL the CONSOLE actually opens.

    ``useRunStream`` opens ``/workspaces/{ws}/runs/{id}/stream``, not the flat
    ``/runs/{id}/stream`` the tests above use. Those two reach the same handler
    only because ``_WorkspaceMiddleware`` rewrites the path — and it rewrites by
    REBUILDING the ASGI scope, which is precisely where a header gets dropped.
    Drop ``Last-Event-ID`` there and every reconnect replays the whole backlog
    again: the duplicated event trail of #1574/#1387, and invisible to every
    flat-path test in this file.

    So this pins the console's own URL, not just the handler underneath it.
    """
    from fastapi.testclient import TestClient

    from cairn.core.triggers.server import build_app

    app = build_app(_StreamingRunner(), enable_scheduler=False)
    with TestClient(app) as client:
        run_id = client.post("/triggers/manual", json={"skill_name": "db-pool"}).json()["run_id"]
        url = f"/workspaces/default/runs/{run_id}/stream"

        with client.stream("GET", url) as s:
            seen = _frame_ids("".join(s.iter_text()))
        assert len(seen) >= 2, f"need a multi-event run to test resumption, got {seen}"

        # Reconnect the way a browser does: the last id it saw, in the header.
        with client.stream("GET", url, headers={"Last-Event-ID": str(seen[-1])}) as s:
            assert _frame_ids("".join(s.iter_text())) == [], \
                "the workspace-scoped path replayed events the client had already seen"

        # A mid-stream resume returns only what came after the cursor.
        with client.stream("GET", url, headers={"Last-Event-ID": str(seen[0])}) as s:
            assert _frame_ids("".join(s.iter_text())) == seen[1:]


async def test_sqlite_append_survives_a_seq_collision(tmp_path):
    """A stale MAX(seq) read (another writer landed in between) trips the
    primary key on INSERT; the store re-reads and retries with the next seq
    instead of losing the event (seen live 2026-09-06 under the in-process
    reporter). Simulated by handing the store a MAX that is one behind, once."""
    path = str(tmp_path / "runs.db")
    store = SqliteRunStore(path)
    await store.create(_run("run-c"))
    await store.append_event("run-c", "running", {})
    await store.append_event("run-c", "metrics", {"seq": 1})
    stale = {"left": 1}

    class _Row:
        def __init__(self, v):
            self.v = v

        def fetchone(self):
            return (self.v,)

    class _Conn:
        """The real connection, with ONE stale MAX(seq) answer."""

        def __init__(self, conn):
            self._c = conn

        def execute(self, sql, *a, **k):
            if sql.startswith("SELECT COALESCE(MAX(seq)") and stale["left"]:
                stale["left"] -= 1
                return _Row(1)      # one behind the truth (2) → seq 2 collides
            return self._c.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._c, name)

    store._conn = _Conn(store._conn)  # type: ignore[assignment]
    ev = await store.append_event("run-c", "metrics", {"seq": 2})
    assert ev.seq == 3
    assert [e.seq for e in await store.get_events("run-c")] == [1, 2, 3]
    store.close()
