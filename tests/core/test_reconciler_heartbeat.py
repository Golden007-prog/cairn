"""ADR-0086 H1 (#1239) — the heartbeat verdict + emit hook on the run reconciler.

The #976 reconciler could only judge runs a Temporal workflow vouched for; a
flow leg on the builtin/SSH engine whose poll loop died (worker crash, leg task
lost in a restart) stayed `running` forever. Legs now stamp `last_heartbeat_at`
(leg_executor) and the reconciler terminalizes a `running` run whose stamp has
expired — publishing the terminal on the bus so its PIPELINE advances/fails
instead of hanging one tier up.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from cairn.core.runtime.reconciler import reconcile_stale_runs
from cairn.core.storage import build_storage
from cairn.core.storage.protocols import Run, TriggerEvent

NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)


async def _none_status(_run_id: str):
    return None  # no runtime to ask — the non-temporal server wiring


def _run(run_id="r1", status="running", *, beat_age_s=None, created_age_s=3600) -> Run:
    return Run(
        id=run_id,
        trigger=TriggerEvent(source="manual", payload={}),
        status=status,
        created_at=(NOW - timedelta(seconds=created_age_s)).isoformat(),
        last_heartbeat_at=(
            (NOW - timedelta(seconds=beat_age_s)).isoformat()
            if beat_age_s is not None else None
        ),
    )


async def _sweep(store, **kw):
    emitted: list[tuple[str, str, dict]] = []

    async def _emit(run_id: str, type_: str, data: dict) -> None:
        emitted.append((run_id, type_, data))

    ids = await reconcile_stale_runs(
        store, _none_status, now=NOW, heartbeat_ttl_seconds=kw.pop("ttl", 300.0),
        emit_fn=_emit, **kw,
    )
    return ids, emitted


async def test_expired_heartbeat_terminalizes_and_emits():
    store = build_storage().runs
    await store.create(_run(beat_age_s=600))          # 10 min silent, TTL 5 min
    ids, emitted = await _sweep(store)
    assert ids == ["r1"]
    run = await store.get("r1")
    assert run.status == "failed"
    assert "orphaned" in (run.error or "")
    # the bus emit is what advances/fails the PIPELINE — store-only would hang it
    assert emitted and emitted[0][0] == "r1" and emitted[0][1] == "failed"
    assert emitted[0][2]["cause"] == "heartbeat"


async def test_fresh_heartbeat_is_left_alone():
    store = build_storage().runs
    await store.create(_run(beat_age_s=30))
    ids, emitted = await _sweep(store)
    assert ids == [] and emitted == []
    assert (await store.get("r1")).status == "running"


async def test_no_heartbeat_is_never_judged_by_silence():
    """Template runs and pre-H1 rows never promised to beat — a missing stamp
    is not evidence of death (the honesty rule the docstring pins)."""
    store = build_storage().runs
    await store.create(_run(beat_age_s=None))
    ids, _ = await _sweep(store)
    assert ids == []
    assert (await store.get("r1")).status == "running"


async def test_awaiting_approval_never_beats_never_dies():
    """A HITL wait has no poll loop by design — the approval reaper (#896) owns
    that lifecycle, not the heartbeat."""
    store = build_storage().runs
    await store.create(_run(status="awaiting_approval", beat_age_s=6000))
    ids, _ = await _sweep(store)
    assert ids == []
    assert (await store.get("r1")).status == "awaiting_approval"


async def test_ttl_zero_disables_the_heartbeat_verdict():
    store = build_storage().runs
    await store.create(_run(beat_age_s=6000))
    ids, _ = await _sweep(store, ttl=0.0)
    assert ids == []


async def test_runtime_verdict_wins_over_heartbeat():
    """When a runtime CAN answer, its answer is authoritative — the heartbeat
    only speaks for runs nothing else can see."""
    store = build_storage().runs
    await store.create(_run(beat_age_s=600))

    async def _temporal_says_done(_run_id: str):
        return "done"

    ids = await reconcile_stale_runs(
        store, _temporal_says_done, now=NOW, heartbeat_ttl_seconds=300.0,
    )
    assert ids == ["r1"]
    run = await store.get("r1")
    assert run.status == "done"          # not the heartbeat's "failed"
    assert run.error in (None, "")        # a runtime `done` is not an error


async def test_grace_window_shields_young_runs():
    store = build_storage().runs
    await store.create(_run(beat_age_s=600, created_age_s=30))  # 30s old run
    ids, _ = await _sweep(store, grace_seconds=120.0)
    assert ids == []


# ── #1429 / #1412 G1: defer to a LIVE durable owner ─────────────────────────

async def test_expired_heartbeat_defers_when_the_durable_owner_is_alive():
    """A Temporal-owned pipeline leg whose workflow is still RUNNING has an
    owner that will retry + re-adopt it (heartbeat-timed activity); a silence
    verdict here would pre-empt that (found live: worker down 10 min →
    reconciler failed the leg at 5 min → the retry honored the failure)."""
    store = build_storage().runs
    await store.create(_run(beat_age_s=600))
    asked: list[str] = []

    async def _alive(run) -> bool:
        asked.append(run.id)
        return True

    ids, emitted = await _sweep(store, owner_alive_fn=_alive)
    assert ids == [] and emitted == [] and asked == ["r1"]
    assert (await store.get("r1")).status == "running"          # left to the owner


async def test_expired_heartbeat_still_fails_when_owner_is_dead_or_unknown():
    store = build_storage().runs
    await store.create(_run("dead", beat_age_s=600))
    await store.create(_run("boom", beat_age_s=600))

    async def _alive(run) -> bool:
        if run.id == "boom":
            raise RuntimeError("temporal unreachable")
        return False

    ids, _ = await _sweep(store, owner_alive_fn=_alive)
    assert sorted(ids) == ["boom", "dead"]                    # unknown owner ⇒ heartbeat rules


async def test_owner_alive_is_only_consulted_for_a_silence_verdict():
    store = build_storage().runs
    await store.create(_run(beat_age_s=10))                   # healthy beat
    asked: list = []

    async def _alive(run) -> bool:
        asked.append(run.id); return True

    ids, _ = await _sweep(store, owner_alive_fn=_alive)
    assert ids == [] and asked == []
