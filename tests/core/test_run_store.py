"""Tests for the Tier 1 SqliteRunStore.

The headline property: state survives a store re-open (restart-safe), which the
in-memory store cannot do. Also exercises the full RunStore contract
so the dispatcher can swap stores without code changes.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.dispatch.dispatcher import RunDispatcher
from cairn.core.storage import (
    Run,
    SqliteRunStore,
)
from cairn.core.engine.schema import TriggerEvent


def _inv(id="run-abc-1", fp="abc", lane="realtime", status="queued"):
    return Run(
        id=id,
        trigger=TriggerEvent(source="pattern", matched_pattern="x", service="svc"),
        lane=lane, fingerprint=fp, status=status,
    )


@pytest.mark.asyncio
async def test_create_get_roundtrip(tmp_path):
    store = SqliteRunStore(str(tmp_path / "inv.db"))
    await store.create(_inv())
    got = await store.get("run-abc-1")
    assert got is not None
    assert got.fingerprint == "abc"
    assert got.trigger.service == "svc"
    store.close()


@pytest.mark.asyncio
async def test_update_patches_and_mirrors_columns(tmp_path):
    store = SqliteRunStore(str(tmp_path / "inv.db"))
    await store.create(_inv())
    updated = await store.update("run-abc-1", status="done", matched_skills=2)
    assert updated.status == "done"
    assert updated.matched_skills == 2
    # status column mirror must move too, so status filtering works
    done = await store.list(status="done")
    assert len(done) == 1
    store.close()


@pytest.mark.asyncio
async def test_full_result_payloads_persist_and_excluded_from_list(tmp_path):
    """#76 DB result backend: the full structured result persists and reads
    back via get(), but the /runs list projection excludes it (cheap listing).
    Additive — the on-disk JSONL trace is untouched."""
    db = str(tmp_path / "inv.db")
    store = SqliteRunStore(db)
    await store.create(_inv())
    payload = {
        "skill_name": "s", "template_name": "t", "template_hash": "h1",
        "rca": {"summary": "root cause"}, "actions": [],
    }
    updated = await store.update("run-abc-1", status="done", result_payloads=[payload])
    assert updated.result_payloads == [payload]

    # survives a reopen (it lives in the store's data blob — no migration)
    store.close()
    reopened = SqliteRunStore(db)
    got = await reopened.get("run-abc-1")
    assert got is not None and got.result_payloads == [payload]

    # GET /runs/{id} projection keeps the full result …
    assert "result_payloads" in got.model_dump(mode="json")
    # … but the /runs LIST projection drops it so listing stays cheap
    listed = (await reopened.list())[0].model_dump(mode="json", exclude={"result_payloads"})
    assert "result_payloads" not in listed
    reopened.close()


@pytest.mark.asyncio
async def test_update_missing_raises_keyerror(tmp_path):
    store = SqliteRunStore(str(tmp_path / "inv.db"))
    with pytest.raises(KeyError):
        await store.update("nope", status="done")
    store.close()


@pytest.mark.asyncio
async def test_state_survives_reopen(tmp_path):
    """The whole point of Tier 1: persistence across process restart."""
    db = str(tmp_path / "inv.db")
    store = SqliteRunStore(db)
    await store.create(_inv(status="running"))
    store.close()

    # Simulate a restart: brand-new store object over the same file.
    reopened = SqliteRunStore(db)
    got = await reopened.get("run-abc-1")
    assert got is not None
    assert got.status == "running"
    reopened.close()


@pytest.mark.asyncio
async def test_find_active_ignores_terminal_and_prefers_oldest(tmp_path):
    store = SqliteRunStore(str(tmp_path / "inv.db"))
    await store.create(_inv(id="old", fp="dup", status="running"))
    await store.create(_inv(id="new", fp="dup", status="queued"))
    await store.create(_inv(id="dead", fp="dup", status="done"))
    active = await store.find_active_by_fingerprint("dup")
    assert active is not None and active.id == "old"  # oldest active
    # a fingerprint with only terminal records → no active match
    await store.update("old", status="done")
    await store.update("new", status="failed")
    assert await store.find_active_by_fingerprint("dup") is None
    store.close()


@pytest.mark.asyncio
async def test_list_filters_and_orders_newest_first(tmp_path):
    store = SqliteRunStore(str(tmp_path / "inv.db"))
    await store.create(_inv(id="a", lane="realtime"))
    await store.create(_inv(id="b", lane="background"))
    assert len(await store.list()) == 2
    rt = await store.list(lane="realtime")
    assert len(rt) == 1 and rt[0].lane == "realtime"
    store.close()


# ─── the store is drop-in for the dispatcher ─────────────────────────


class _FakeRunner:
    async def run(self, event, *, environment=None, event_sink=None, run_id=None, _cascade_depth=0):
        import types
        return [types.SimpleNamespace(
            skill_name="s", template_name="t", duration_seconds=0.0, actions=[],
        )]


@pytest.mark.asyncio
async def test_dispatcher_works_with_sqlite_store(tmp_path):
    store = SqliteRunStore(str(tmp_path / "inv.db"))
    d = RunDispatcher(_FakeRunner(), store=store)
    inv = await d.submit(
        TriggerEvent(source="pattern", matched_pattern="boom", service="svc"),
        wait=True,
    )
    assert inv.status == "done"
    # and it's durably on disk
    assert (await store.get(inv.id)).status == "done"
    store.close()


# ─── #86: the hot path runs sqlite off the event loop ────────────────


@pytest.mark.asyncio
async def test_methods_do_not_block_the_event_loop(tmp_path):
    """#86: every async method runs its sqlite3 work via asyncio.to_thread, so
    the run-store hot path doesn't stall the event loop. Construct + seed
    OUTSIDE the guard (construction is one-time startup, deliberately sync),
    then exercise every method under blockbuster — a sync sqlite3/os call left
    on the loop would raise BlockingError and fail this test."""
    bb = pytest.importorskip("blockbuster")  # dev extra; skip cleanly if absent
    store = SqliteRunStore(str(tmp_path / "inv.db"))   # __init__ (unguarded startup)
    await store.create(_inv(id="run-abc-1", fp="abc", status="queued"))

    with bb.blockbuster_ctx(["cairn"]):
        assert (await store.get("run-abc-1")) is not None
        await store.update("run-abc-1", status="running", matched_skills=1)
        await store.append_event("run-abc-1", "skill_started", {"skill_name": "t"})
        assert len(await store.list()) >= 1
        assert await store.find_active_by_fingerprint("abc") is not None
        assert len(await store.get_events("run-abc-1")) == 1

    store.close()
