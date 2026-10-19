"""#1531 — the per-target concurrency count is a STORE-SIDE aggregate.

The H5 cap (#1247) used to be computed by listing the 500 newest runs per
slot-holding status and filtering them in Python. That is wrong in two ways
this file pins:

  1. **It is per-process.** Two control-plane replicas each counted only what
     they could see, so a cap of N admitted up to N per replica. The fix is
     that both ask the same database the same question — proven for real in
     the live-PG tests below, and structurally by every tier answering from
     its own storage rather than from a hydrated list.
  2. **It truncated at 500** — and truncation UNDER-counts, which admits a leg
     the cap should have held. `test_*_above_the_legacy_scan_ceiling` is the
     regression: the old algorithm cannot pass it.

The fallback path matters as much as the fast path: a store predating the
method must keep working, because a concurrency knob that raises would fail
a leg over bookkeeping.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.dispatch.target_concurrency import (
    IN_FLIGHT_STATUSES,
    in_flight_on_target,
)
from cairn.core.engine.schema import TriggerEvent
from cairn.core.storage.memory.runs import InMemoryRunStore
from cairn.core.storage.protocols import (
    SCAN_COUNT_LIMIT,
    Run,
    scan_in_flight_on_target,
)
from cairn.core.storage.sqlite.runs import SqliteRunStore


def _run(rid: str, *, target: str, status: str = "running") -> Run:
    return Run(id=rid, trigger=TriggerEvent(source="manual"), status=status,
               compute_target=target)


async def _seed(store, runs) -> None:
    for r in runs:
        await store.create(r)


@pytest.fixture
def sqlite_store(tmp_path):
    return SqliteRunStore(str(tmp_path / "runs.db"))


# ── the shape every tier must agree on ───────────────────────────────────


@pytest.mark.parametrize("tier", ["memory", "sqlite"])
async def test_counts_only_matching_target_and_status(tier, tmp_path, sqlite_store):
    store = InMemoryRunStore() if tier == "memory" else sqlite_store
    await _seed(store, [
        _run("a", target="gpu-1"),
        _run("b", target="gpu-1", status="awaiting_approval"),
        _run("c", target="gpu-2"),                       # other target
        _run("d", target="gpu-1", status="done"),        # terminal — no slot
        _run("e", target="", status="running"),          # untargeted
    ])
    assert await store.count_in_flight_on_target("gpu-1", IN_FLIGHT_STATUSES) == 2
    assert await store.count_in_flight_on_target("gpu-2", IN_FLIGHT_STATUSES) == 1
    assert await store.count_in_flight_on_target("nope", IN_FLIGHT_STATUSES) == 0


@pytest.mark.parametrize("tier", ["memory", "sqlite"])
async def test_excludes_the_asking_run(tier, sqlite_store):
    """A leg waiting for a slot must not be blocked by its own row."""
    store = InMemoryRunStore() if tier == "memory" else sqlite_store
    await _seed(store, [_run("self", target="gpu-1"), _run("other", target="gpu-1")])
    assert await store.count_in_flight_on_target(
        "gpu-1", IN_FLIGHT_STATUSES, exclude_run_id="self") == 1


@pytest.mark.parametrize("tier", ["memory", "sqlite"])
async def test_queued_does_not_hold_a_slot(tier, sqlite_store):
    """`queued` is WAITING for a slot — counting it would make the cap
    self-blocking, which is why statuses are the caller's policy."""
    store = InMemoryRunStore() if tier == "memory" else sqlite_store
    await _seed(store, [_run("q", target="gpu-1", status="queued")])
    assert await store.count_in_flight_on_target("gpu-1", IN_FLIGHT_STATUSES) == 0


# ── the regression the old algorithm cannot pass ─────────────────────────


@pytest.mark.parametrize("tier", ["memory", "sqlite"])
async def test_is_exact_above_the_legacy_scan_ceiling(tier, sqlite_store):
    """THE #1531 bug: the pre-fix scan read 500 runs per status and filtered
    them here, so run 501 was invisible — an UNDER-count, i.e. an over-admit.
    """
    store = InMemoryRunStore() if tier == "memory" else sqlite_store
    n = SCAN_COUNT_LIMIT + 25
    await _seed(store, [_run(f"r{i}", target="gpu-1") for i in range(n)])

    assert await store.count_in_flight_on_target("gpu-1", IN_FLIGHT_STATUSES) == n
    assert await in_flight_on_target(store, "gpu-1") == n

    # and the algorithm it replaced demonstrably cannot reach that number
    legacy = await scan_in_flight_on_target(store, "gpu-1", IN_FLIGHT_STATUSES)
    assert legacy == SCAN_COUNT_LIMIT < n, (
        "if the legacy scan is no longer capped this test is not proving anything")


# ── compatibility: a store predating the method still works ──────────────


class _LegacyStore:
    """A duck-typed store with `list` and no `count_in_flight_on_target` — an
    out-of-tree tier, or a test stub written before #1531."""

    def __init__(self, runs):
        self._runs = list(runs)

    async def list(self, *, status=None, limit=100):
        return [r for r in self._runs if r.status == status][:limit]


async def test_a_store_without_the_method_falls_back_instead_of_raising():
    """A concurrency knob must never be able to fail a leg over bookkeeping."""
    store = _LegacyStore([_run("a", target="gpu-1"), _run("b", target="gpu-1"),
                          _run("c", target="gpu-2")])
    assert await in_flight_on_target(store, "gpu-1") == 2


async def test_a_store_whose_method_rejects_the_call_also_falls_back():
    class _WrongSignature(_LegacyStore):
        async def count_in_flight_on_target(self, target):  # missing statuses
            raise AssertionError("should not be reachable with this signature")

    store = _WrongSignature([_run("a", target="gpu-1")])
    assert await in_flight_on_target(store, "gpu-1") == 1


async def test_empty_target_is_not_a_wildcard():
    """An unstamped leg must count as 'no target', never as 'every target'."""
    store = InMemoryRunStore()
    await _seed(store, [_run("a", target=""), _run("b", target="gpu-1")])
    assert await in_flight_on_target(store, "") == 0
    assert await store.count_in_flight_on_target("", IN_FLIGHT_STATUSES) == 0


# ── sqlite: the lifted column is real, written, and backfilled ───────────


async def test_sqlite_writes_the_lifted_column_on_create_and_update(sqlite_store):
    await _seed(sqlite_store, [_run("a", target="gpu-1")])
    row = sqlite_store._conn.execute(
        "SELECT compute_target FROM runs WHERE id='a'").fetchone()
    assert row[0] == "gpu-1", "the column must be written, not just the JSON blob"

    await sqlite_store.update("a", compute_target="gpu-9")
    row = sqlite_store._conn.execute(
        "SELECT compute_target, data FROM runs WHERE id='a'").fetchone()
    assert row[0] == "gpu-9", "an update must keep the column in step with the blob"
    assert '"compute_target":"gpu-9"' in row[1].replace(" ", ""), "blob too"
    # and the count follows the move, not the stale column
    assert await sqlite_store.count_in_flight_on_target("gpu-1", IN_FLIGHT_STATUSES) == 0
    assert await sqlite_store.count_in_flight_on_target("gpu-9", IN_FLIGHT_STATUSES) == 1


async def test_sqlite_stores_null_not_empty_string_for_untargeted_runs(sqlite_store):
    """The index is partial (`WHERE compute_target IS NOT NULL`), so an
    untargeted run must not carry '' into it."""
    await _seed(sqlite_store, [_run("a", target="")])
    row = sqlite_store._conn.execute(
        "SELECT compute_target FROM runs WHERE id='a'").fetchone()
    assert row[0] is None


# ── live Postgres (the fleet-wide half) ──────────────────────────────────
#
# Gated on PGSTORE_DSN like the rest of the repo's live-PG coverage. Two
# independent stores against one database are the honest single-process
# stand-in for two replicas; the literal two-control-plane acceptance is a
# box-session step (#1531's acceptance line).


def _pg_dsn():
    dsn = os.environ.get("PGSTORE_DSN")
    if not dsn:
        pytest.skip("PGSTORE_DSN not set — skipping live Postgres integration")
    return dsn


@pytest.fixture
async def pg_store():
    dsn = _pg_dsn()
    pytest.importorskip("asyncpg")
    from cairn.core.storage.postgres.runs import PostgresRunStore

    # No explicit init — the schema is ensured lazily inside _get_pool (same
    # contract test_event_seq_race relies on). The first version of this
    # fixture called a guessed `_ensure()` that does not exist, and the gate
    # meant it never ran until the first REAL box session — the exact
    # "always-stubbed seam" failure mode this repo keeps re-learning.
    yield PostgresRunStore(dsn=dsn)


async def test_live_pg_counts_are_exact_above_the_scan_ceiling(pg_store):
    import uuid

    target = f"gpu-{uuid.uuid4().hex[:8]}"
    n = SCAN_COUNT_LIMIT + 25
    await _seed(pg_store, [_run(f"{target}-{i}", target=target) for i in range(n)])
    assert await pg_store.count_in_flight_on_target(target, IN_FLIGHT_STATUSES) == n


async def test_live_pg_two_stores_see_one_shared_count(pg_store):
    """The replica question: a run admitted through one store is visible to the
    other's count immediately — that is what makes a cap of N mean N fleetwide."""
    import uuid

    from cairn.core.storage.postgres.runs import PostgresRunStore

    target = f"gpu-{uuid.uuid4().hex[:8]}"
    other = PostgresRunStore(dsn=_pg_dsn())  # second pool = the "other replica"

    await _seed(pg_store, [_run(f"{target}-1", target=target)])
    assert await other.count_in_flight_on_target(target, IN_FLIGHT_STATUSES) == 1

    await _seed(other, [_run(f"{target}-2", target=target)])
    assert await pg_store.count_in_flight_on_target(target, IN_FLIGHT_STATUSES) == 2
    assert await pg_store.count_in_flight_on_target(
        target, IN_FLIGHT_STATUSES, exclude_run_id=f"{target}-1") == 1
