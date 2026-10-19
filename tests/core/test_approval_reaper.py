"""#896 — HITL approval-gate lifecycle: TTL auto-expire + bulk-dismiss.

Exercises the reaper against the REAL InMemoryRunStore: a stale awaiting_approval
run is transitioned to `expired` with an audited event; fresh gates, sandbox runs,
and already-resolved runs are left alone; and the last-event timestamp (not
created_at) is the age basis. Bulk-dismiss expires named gates and skips the rest.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cairn.core.approval.reaper import (
    dismiss_runs,
    expire_stale_approvals,
    ttl_hours_from_env,
)
from cairn.core.engine.schema import TriggerEvent
from cairn.core.storage.protocols import Run

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


async def _mk(store, run_id, *, status="awaiting_approval", age_hours=0.0, sandbox=False):
    created = _iso(NOW - timedelta(hours=age_hours))
    run = Run(
        id=run_id, trigger=TriggerEvent(source="manual"), status=status,
        created_at=created, started_at=created, sandbox=sandbox,
    )
    await store.create(run)
    return run


@pytest.fixture
def store():
    from cairn.core.storage.memory.runs import InMemoryRunStore
    return InMemoryRunStore()


async def test_stale_gate_is_expired_and_audited(store):
    await _mk(store, "run-stale", age_hours=200)  # > 168h TTL
    expired = await expire_stale_approvals(store, ttl_hours=168, now=NOW)
    assert expired == ["run-stale"]

    run = await store.get("run-stale")
    assert run.status == "expired"
    assert run.ended_at is not None
    assert "168h" in (run.error or "")
    # audited on the append-only log — the governance receipt.
    evs = await store.get_events("run-stale")
    assert any(e.type == "expired" and e.data.get("prior_status") == "awaiting_approval" for e in evs)


async def test_fresh_gate_is_kept(store):
    await _mk(store, "run-fresh", age_hours=1)  # well within TTL
    expired = await expire_stale_approvals(store, ttl_hours=168, now=NOW)
    assert expired == []
    assert (await store.get("run-fresh")).status == "awaiting_approval"


async def test_sandbox_run_is_excluded(store):
    # #877 — Playground runs are ephemeral; never reap them even when ancient.
    await _mk(store, "run-sandbox", age_hours=500, sandbox=True)
    expired = await expire_stale_approvals(store, ttl_hours=168, now=NOW)
    assert expired == []
    assert (await store.get("run-sandbox")).status == "awaiting_approval"


async def test_non_awaiting_runs_are_untouched(store):
    # A long-running / done run is never a gate — the list filter + expire_run guard
    # both protect it.
    await _mk(store, "run-running", status="running", age_hours=500)
    await _mk(store, "run-done", status="done", age_hours=500)
    expired = await expire_stale_approvals(store, ttl_hours=168, now=NOW)
    assert expired == []
    assert (await store.get("run-running")).status == "running"
    assert (await store.get("run-done")).status == "done"


async def test_ttl_zero_disables_the_sweep(store):
    await _mk(store, "run-old", age_hours=9000)
    assert await expire_stale_approvals(store, ttl_hours=0, now=NOW) == []
    assert (await store.get("run-old")).status == "awaiting_approval"


async def test_last_event_ts_is_the_age_basis_not_created_at(store):
    # An OLD run (created 300h ago) that just emitted an event is NOT stale — the
    # last activity, not birth, decides. (Guards the long-run-then-pauses case.)
    await _mk(store, "run-active", age_hours=300)
    await store.append_event("run-active", "awaiting_approval", {})  # ts defaults to real now
    expired = await expire_stale_approvals(store, ttl_hours=168, now=datetime.now(timezone.utc))
    assert expired == []
    assert (await store.get("run-active")).status == "awaiting_approval"


async def test_bulk_dismiss_expires_named_gates_and_skips_the_rest(store):
    await _mk(store, "g1", age_hours=2)   # fresh but explicitly dismissed
    await _mk(store, "g2", age_hours=2)
    await _mk(store, "done", status="done", age_hours=2)  # already resolved
    out = await dismiss_runs(store, ["g1", "g2", "done", "missing"], now=NOW, actor="alice")
    assert set(out["expired"]) == {"g1", "g2"}
    assert set(out["skipped"]) == {"done", "missing"}
    assert (await store.get("g1")).status == "expired"
    assert "alice" in ((await store.get("g1")).error or "")
    assert (await store.get("done")).status == "done"  # untouched


async def test_ttl_from_env_default_and_override(monkeypatch):
    monkeypatch.delenv("CAIRN_APPROVAL_TTL_HOURS", raising=False)
    assert ttl_hours_from_env() == 168.0
    monkeypatch.setenv("CAIRN_APPROVAL_TTL_HOURS", "24")
    assert ttl_hours_from_env() == 24.0
    monkeypatch.setenv("CAIRN_APPROVAL_TTL_HOURS", "garbage")
    assert ttl_hours_from_env() == 168.0  # bad value → safe default
