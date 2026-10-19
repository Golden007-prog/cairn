"""One bad row must not take down the listing (#1601).

Found for real: a run whose `results` held `{'stats': {}, 'metrics': {}}`
instead of a list made `GET /runs` return 500 for EVERY run — 38 healthy rows
unreachable because of 1 corrupt one. The console rendered that as "No runs
yet", so a user who had just completed a run was told they had never run
anything.

The row got there because `model_copy(update=...)` does not validate: the
wrong shape was written, serialized, and only raised on the way back out, in
another process, unattributable.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest
from pydantic import ValidationError

from cairn.core.storage.hydration import hydrate_all, skipped_rows, validated_update
from cairn.core.storage.protocols import Run, TriggerEvent


def _run(rid: str) -> Run:
    return Run(id=rid, trigger=TriggerEvent(source="manual", payload={}))


# ── read side: survive what is already on disk ───────────────────────────────

def test_one_corrupt_row_does_not_take_down_the_listing():
    """THE bug. The old code was `[_hydrate(r) for r in rows]`."""
    rows = ["good-1", "CORRUPT", "good-2"]

    def hydrate(r):
        if r == "CORRUPT":
            raise ValueError("results: Input should be a valid array")
        return _run(r)

    out = hydrate_all(rows, hydrate, source="test", row_id=lambda r: str(r))
    assert [r.id for r in out.items] == ["good-1", "good-2"], "healthy rows survive"
    assert out.skipped == 1


def test_the_listing_says_how_many_it_could_not_read():
    """Silently dropping a row would make the list quietly wrong — "I have 39
    runs and see 38" has to be answerable."""
    def hydrate(r):
        raise ValueError("nope")

    assert hydrate_all(["a", "b"], hydrate, source="test") == ([], 2)


def test_a_clean_listing_reports_nothing_skipped():
    out = hydrate_all(["x"], _run, source="test")
    assert [r.id for r in out.items] == ["x"] and out.skipped == 0


def test_the_bad_row_is_named_in_the_log(caplog):
    """An operator has to be able to find and fix the row."""
    def hydrate(r):
        raise ValueError("boom")

    with caplog.at_level("WARNING"):
        hydrate_all(["run-abc123"], hydrate, source="runs", row_id=lambda r: str(r))
    assert "run-abc123" in caplog.text


def test_an_unreadable_id_still_skips_cleanly():
    """The id lookup itself must not become a second way to crash the listing."""
    def bad_id(_r):
        raise RuntimeError("cannot read id")

    out = hydrate_all(["x"], lambda _r: (_ for _ in ()).throw(ValueError("bad")),
                      source="test", row_id=bad_id)
    assert out.items == [] and out.skipped == 1


# ── write side: stop making new ones ─────────────────────────────────────────

def test_model_copy_really_does_skip_validation():
    """Pins the mechanism this fix exists for — if pydantic ever starts
    validating here, `validated_update` becomes redundant and this test says so."""
    bad = _run("r").model_copy(update={"results": {"stats": {}}})
    assert bad.results == {"stats": {}}, "model_copy wrote a dict into list[RunResult]"


def test_a_wrong_shaped_write_is_refused_at_the_callsite():
    """The exact corruption, rejected where it is attributable."""
    with pytest.raises(ValidationError) as exc:
        validated_update(_run("r"), {"results": {"stats": {}, "metrics": {}}})
    assert "results" in str(exc.value)


def test_a_normal_update_still_works():
    updated = validated_update(_run("r"), {"status": "failed", "error": "boom"})
    assert (updated.status, updated.error) == ("failed", "boom")


def test_the_hot_heartbeat_update_is_unaffected():
    updated = validated_update(_run("r"), {"last_heartbeat_at": "2026-08-24T00:00:00Z"})
    assert updated.last_heartbeat_at == "2026-08-24T00:00:00Z"


def test_an_unknown_key_passes_through_untouched():
    """Narrow the change to the failure it was built for — a KNOWN field given
    the wrong shape — rather than altering update semantics for everyone."""
    updated = validated_update(_run("r"), {"not_a_field": 1})
    assert updated.id == "r"


def test_a_valid_results_list_is_accepted():
    ok = validated_update(_run("r"), {"results": []})
    assert ok.results == []


# ── the two halves meet ──────────────────────────────────────────────────────

def test_a_row_written_before_the_guard_is_still_readable_as_skipped():
    """Write-side validation cannot fix rows already on disk, which is why the
    read side has to be defensive too."""
    corrupt_blob = json.dumps({"id": "old", "trigger": {"source": "manual", "payload": {}},
                               "results": {"stats": {}}})

    def hydrate(blob):
        return Run.model_validate_json(blob)

    out = hydrate_all([corrupt_blob], hydrate, source="test")
    assert out.items == [] and out.skipped == 1


# ── the count has to survive the worker thread ───────────────────────────────

@pytest.mark.asyncio
async def test_the_skip_count_reaches_the_request_through_a_real_store():
    """This is the bug my first attempt shipped: `hydrate_all` set the
    ContextVar itself, and sqlite hydrates under `asyncio.to_thread`, which
    hands the worker a COPY of the context — so the set was discarded on the
    way out. It worked on Postgres (called inline) and silently did nothing on
    sqlite, which is worse than not working at all. Caught by checking the live
    endpoint, not by a unit test that ran in a single context.

    Hence the store publishes the count from its ASYNC `list`, and this asserts
    that end-to-end through a real SqliteRunStore rather than through the
    helper in isolation.
    """
    from cairn.core.storage.sqlite.runs import SqliteRunStore

    store = SqliteRunStore(path=":memory:")
    await store.create(_run("healthy"))
    # write a row that cannot be rebuilt, the way the real one got there
    store._conn.execute(
        "INSERT INTO runs (id, fingerprint, lane, status, workspace_id, created_at, data) "
        "VALUES (?,?,?,?,?,?,?)",
        ("corrupt", "", "realtime", "failed", "default", "2026-01-01T00:00:00Z",
         json.dumps({"id": "corrupt", "trigger": {"source": "manual", "payload": {}},
                     "results": {"stats": {}}})),
    )
    store._conn.commit()

    runs = await store.list()
    assert [r.id for r in runs] == ["healthy"], "the corrupt row is skipped, not fatal"
    assert skipped_rows() == 1, "and the count crossed the to_thread boundary"


@pytest.mark.asyncio
async def test_a_clean_store_reports_no_skips_through_the_async_path():
    from cairn.core.storage.sqlite.runs import SqliteRunStore

    store = SqliteRunStore(path=":memory:")
    await store.create(_run("a"))
    assert len(await store.list()) == 1
    assert skipped_rows() == 0
