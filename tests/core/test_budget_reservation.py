"""Atomic budget reserve-at-submit (#1094, Q16).

TWO defects, only one of which the issue names:

1. **The race it describes** — budget enforcement is check-then-submit, so two
   doors submitting concurrently when the remaining budget covers only one BOTH
   pass. Failure mode: "paid for what policy said we couldn't."

2. **`est_usd` is never passed** (found while confirming #1: `dispatcher.py:605`
   and `run.py:275` both call ``series_budget_exceeded(series_id)`` with no
   estimate, while the function's own docstring promises
   "already-spent + this run's estimate > budget"). The gate therefore degrades
   to ``spent > budget`` — purely REACTIVE, blocking only after a series has
   ALREADY overspent. A $300 series admits a $500 run as long as nothing has
   been spent yet.

The fix is one primitive: reserve the estimate atomically at admission, then
release on failure/cancel or settle to actuals on success.

#1530 (ADR-0091 tier 2) added the BACKEND SEAM under that primitive — memory by
default, shared Postgres when ``OBS_STORAGE=postgres://…`` — and #1325's
lifecycle leak (``run_template`` reserved and never closed) is folded in here
too. The live-PG half of this file is skip-gated on ``PGSTORE_DSN``.
"""
from __future__ import annotations

import asyncio
import os

import pytest

import cairn.core.budget_reservations as B
from cairn.core.budget_reservations import (
    BudgetExceeded,
    Reservation,
    held_for_run,
    release,
    reserve,
    reserved_total,
    reset_reservations,
    settle,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # #1530 — these are the MEMORY-backend tests, so pin the selector: a
    # developer (or box) with OBS_STORAGE=postgres://… exported would otherwise
    # silently route them at a live database. The live-PG tests below build
    # their backend explicitly and are unaffected; the selector test re-sets the
    # var itself, which wins over this delenv.
    monkeypatch.delenv("OBS_STORAGE", raising=False)
    reset_reservations()
    yield
    reset_reservations()


# ── defect 2: the estimate must count ────────────────────────────────────────

async def test_estimate_is_counted_against_the_cap():
    """A $500 estimate against a $300 cap must be refused even at ZERO spend —
    the reactive gate admitted it."""
    with pytest.raises(BudgetExceeded) as e:
        await reserve("s1", est_usd=500.0, spent_usd=0.0, budget_usd=300.0)
    assert "500" in str(e.value) and "300" in str(e.value)


async def test_an_affordable_estimate_is_admitted():
    r = await reserve("s1", est_usd=50.0, spent_usd=100.0, budget_usd=300.0)
    assert isinstance(r, Reservation) and r.amount_usd == 50.0


async def test_spent_plus_estimate_is_the_gate_not_spent_alone():
    await reserve("s1", est_usd=250.0, spent_usd=100.0, budget_usd=300.0) if False else None
    with pytest.raises(BudgetExceeded):
        await reserve("s1", est_usd=250.0, spent_usd=100.0, budget_usd=300.0)


async def test_zero_budget_means_uncapped():
    """An uncapped series never blocks — today's semantics, preserved."""
    r = await reserve("s1", est_usd=10_000.0, spent_usd=0.0, budget_usd=0.0)
    assert r.amount_usd == 10_000.0


async def test_a_zero_estimate_still_reserves_nothing_but_succeeds():
    r = await reserve("s1", est_usd=0.0, spent_usd=299.0, budget_usd=300.0)
    assert r.amount_usd == 0.0


# ── defect 1: the race ───────────────────────────────────────────────────────

async def test_two_concurrent_submits_for_a_budget_of_one():
    """THE acceptance test. Budget covers exactly one $60 run; fire both at
    once. Exactly one proceeds — the other raises deterministically."""
    results = await asyncio.gather(
        reserve("s1", est_usd=60.0, spent_usd=0.0, budget_usd=100.0),
        reserve("s1", est_usd=60.0, spent_usd=0.0, budget_usd=100.0),
        return_exceptions=True,
    )
    ok = [r for r in results if isinstance(r, Reservation)]
    refused = [r for r in results if isinstance(r, BudgetExceeded)]
    assert len(ok) == 1 and len(refused) == 1, results


async def test_many_concurrent_submits_never_exceed_the_cap():
    """Ten $30 submits against a $100 cap: at most three may win, and the total
    reserved must never exceed the budget."""
    out = await asyncio.gather(*[
        reserve("s1", est_usd=30.0, spent_usd=0.0, budget_usd=100.0)
        for _ in range(10)
    ], return_exceptions=True)
    winners = [r for r in out if isinstance(r, Reservation)]
    assert len(winners) == 3, f"{len(winners)} winners — cap allows 3"
    assert (await reserved_total("s1")) <= 100.0


async def test_reservations_are_per_series():
    await reserve("s1", est_usd=90.0, spent_usd=0.0, budget_usd=100.0)
    r = await reserve("s2", est_usd=90.0, spent_usd=0.0, budget_usd=100.0)
    assert r.amount_usd == 90.0, "one series must not consume another's budget"


# ── release / settle ─────────────────────────────────────────────────────────

async def test_release_frees_the_reservation_for_the_next_submit():
    r = await reserve("s1", est_usd=90.0, spent_usd=0.0, budget_usd=100.0)
    with pytest.raises(BudgetExceeded):
        await reserve("s1", est_usd=90.0, spent_usd=0.0, budget_usd=100.0)
    await release(r)
    assert (await reserve("s1", est_usd=90.0, spent_usd=0.0, budget_usd=100.0)).amount_usd == 90.0


async def test_settle_drops_the_reservation_because_spend_is_now_real():
    """After settling, the amount lives in the ledger's SPENT — keeping the
    reservation too would double-count it."""
    r = await reserve("s1", est_usd=90.0, spent_usd=0.0, budget_usd=100.0)
    await settle(r, actual_usd=40.0)
    assert (await reserved_total("s1")) == 0.0


async def test_release_is_idempotent():
    """Terminal handling can fire twice (reconciler + completion path); a double
    release must not free budget that was never held."""
    r = await reserve("s1", est_usd=50.0, spent_usd=0.0, budget_usd=100.0)
    await release(r)
    await release(r)
    assert (await reserved_total("s1")) == 0.0


async def test_settling_an_already_released_reservation_is_safe():
    r = await reserve("s1", est_usd=50.0, spent_usd=0.0, budget_usd=100.0)
    await release(r)
    await settle(r, actual_usd=10.0)
    assert (await reserved_total("s1")) == 0.0


# ── integration with the existing check ──────────────────────────────────────

async def test_series_budget_exceeded_accounts_for_live_reservations():
    """The read-side must see reservations, or the console shows budget that is
    already committed (the issue's third acceptance point)."""
    from cairn.core.budget_reservations import remaining_usd

    await reserve("s1", est_usd=70.0, spent_usd=10.0, budget_usd=100.0)
    assert (await remaining_usd("s1", spent_usd=10.0, budget_usd=100.0)) == pytest.approx(20.0)


async def test_remaining_is_never_negative():
    from cairn.core.budget_reservations import remaining_usd

    assert (await remaining_usd("s1", spent_usd=500.0, budget_usd=100.0)) == 0.0


async def test_uncapped_remaining_is_none():
    from cairn.core.budget_reservations import remaining_usd

    assert (await remaining_usd("s1", spent_usd=5.0, budget_usd=0.0)) is None


# ── the dispatcher actually uses it (not a library nobody calls) ─────────────

async def test_dispatcher_refuses_a_run_that_would_breach_the_series_cap(monkeypatch):
    """End-to-end at the admission door: a $500 estimate into a $300 series is
    refused BEFORE the run record exists. Under the old reactive gate this was
    admitted, because nothing had been spent yet. The check is a ``run.admitting``
    provider's now (#2421 decision 6); its refusal is the dispatcher's ValueError."""
    from cairn.core.dispatch import dispatcher as D

    _stub_series(monkeypatch, budget_usd=300.0)

    disp = D.RunDispatcher(runner=object(), store=_FakeStore())
    with pytest.raises(ValueError) as e:
        await disp.run_template(
            "demo/tmpl", inputs={}, series_id="s-cap", estimated_usd=500.0)
    assert "300" in str(e.value) and "500" in str(e.value)


class _FakeStore:
    """Minimal store. For the refusal test the point is that create() is never
    reached; the #1325 lifecycle tests below drive a whole run through it."""

    def __init__(self):
        self.created = []
        self.updates = []

    async def create(self, run):
        self.created.append(run)
        return run

    async def update(self, run_id=None, **kw):
        self.updates.append((run_id, kw))
        return None

    async def append_event(self, *a, **kw):
        return None

    async def get_events(self, *a, **kw):
        return []

    async def get(self, run_id, *a, **kw):
        return None


# ── #1530: the backend seam ─────────────────────────────────────────────────


@pytest.mark.parametrize("url,expect", [
    ("postgres://u:p@h/db", B._PostgresBackend),
    ("postgresql://u:p@h/db", B._PostgresBackend),
    ("sqlite://:memory:", B._MemoryBackend),
    ("", B._MemoryBackend),
])
def test_backend_honors_obs_storage(monkeypatch, url, expect):
    """One env var keeps meaning "shared state lives in PG" — the same selector
    every other store uses (#427). No OBS_STORAGE at all → memory, so
    single-process dev needs zero configuration."""
    monkeypatch.setenv("OBS_STORAGE", url)
    reset_reservations()
    try:
        assert isinstance(B._backend(), expect)
    finally:
        reset_reservations()


async def test_held_for_run_finds_the_claim_by_run_id():
    """The lookup that lets a terminal handler close a reservation it never
    held — a resumed HITL run, or (under Postgres) another replica."""
    r = await reserve("s1", est_usd=10.0, spent_usd=0.0, budget_usd=100.0, run_id="run-a")
    await reserve("s1", est_usd=10.0, spent_usd=0.0, budget_usd=100.0, run_id="run-b")
    found = await held_for_run("run-a")
    assert [x.id for x in found] == [r.id]
    await release(r)
    assert (await held_for_run("run-a")) == []


async def test_held_for_run_ignores_an_empty_run_id():
    await reserve("s1", est_usd=10.0, spent_usd=0.0, budget_usd=100.0)
    assert (await held_for_run("")) == []


# ── #1325: every path that reserves must settle or release ──────────────────


def _stub_template_execution(monkeypatch, *, output):
    """Make ``_run_template_task`` runnable without a real pack: the compile /
    resolve / execute trio is lazily imported INSIDE the method, so patching the
    modules' attributes is enough."""
    import cairn.core.engine.compiler as C
    import cairn.core.engine.runner as R
    import cairn.core.engine.skill_engine as SE
    import cairn.core.operator as OP
    import cairn.core.pack.loader as PL

    monkeypatch.setattr(SE, "resolve_template_path", lambda ref: "/nonexistent.yaml")
    monkeypatch.setattr(C, "compile_template", lambda path: object())
    monkeypatch.setattr(OP, "load_all_operators", lambda **kw: None)
    monkeypatch.setattr(PL, "load_all_packs", lambda **kw: None)

    async def _run(compiled, inputs):
        if isinstance(output, Exception):
            raise output
        return output

    monkeypatch.setattr(R, "run_within_budget", _run)


def _stub_series(monkeypatch, *, budget_usd=300.0, spent_usd=0.0):
    """A stand-in ``run.admitting`` provider (#2421 decision 6). The real one is the
    platform's ``series`` provider, outside the public set; this one reserves the
    estimate the same way, so these tests prove the CORE half — the dispatcher turns
    a refusal into its ValueError and closes the claim BY RUN ID on every terminal."""
    from cairn.core.pack import run_hooks as RH

    async def _series(event, payload):
        if event != RH.RUN_ADMITTING or not payload["series_id"]:
            return
        try:
            await reserve(payload["series_id"], est_usd=payload["estimated_usd"], spent_usd=spent_usd,
                          budget_usd=budget_usd, run_id=payload["run_id"])
        except BudgetExceeded as e:
            raise RH.AdmissionRefused(str(e)) from None

    monkeypatch.setattr(RH, "load_run_hooks", lambda: {"series": _series})


async def _drive(disp, run):
    task = disp._tasks.get(run.id)
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)


async def test_a_failed_template_run_releases_its_reservation(monkeypatch):
    """#1325, THE regression this fold-in closes. Before it, ``run_template``
    reserved at admission and nothing ever settled or released — a failed run
    held its estimate forever, and enough failures walled the series off from
    admitting anything at all."""
    from cairn.core.dispatch import dispatcher as D

    _stub_series(monkeypatch)
    _stub_template_execution(monkeypatch, output=RuntimeError("boom"))

    disp = D.RunDispatcher(runner=object(), store=_FakeStore())
    run = await disp.run_template(
        "demo/tmpl", inputs={}, series_id="s-fail", estimated_usd=50.0)
    await _drive(disp, run)

    assert (await reserved_total("s-fail")) == 0.0, "failed run leaked its hold"
    assert (await held_for_run(run.id)) == []


async def test_a_completed_template_run_settles_its_reservation(monkeypatch):
    """Success closes the hold too — the actual cost is in the ledger now, so
    keeping the estimate as well would double-count it."""
    from cairn.core.dispatch import dispatcher as D

    _stub_series(monkeypatch)
    _stub_template_execution(monkeypatch, output={"output": "ok"})

    disp = D.RunDispatcher(runner=object(), store=_FakeStore())
    run = await disp.run_template(
        "demo/tmpl", inputs={}, series_id="s-ok", estimated_usd=50.0)
    await _drive(disp, run)

    assert (await reserved_total("s-ok")) == 0.0


async def test_a_run_paused_at_a_gate_KEEPS_its_reservation(monkeypatch):
    """A pause is not a terminal: the run is still in flight and its estimate
    must still count against the cap. Releasing here would let a second run in
    on budget the paused one is about to spend."""
    from cairn.core.dispatch import dispatcher as D

    _stub_series(monkeypatch)
    _stub_template_execution(
        monkeypatch, output={"__interrupt__": [{"kind": "approval"}]})

    disp = D.RunDispatcher(runner=object(), store=_FakeStore())
    run = await disp.run_template(
        "demo/tmpl", inputs={}, series_id="s-pause", estimated_usd=50.0)
    await _drive(disp, run)

    assert (await reserved_total("s-pause")) == 50.0
    assert [r.run_id for r in await held_for_run(run.id)] == [run.id]


async def test_a_failed_admission_after_the_claim_releases_it(monkeypatch):
    """The claim is taken before the run record exists. If persisting the run
    then fails, nothing will ever run to close the hold — so admission must
    release it on its way out."""
    from cairn.core.dispatch import dispatcher as D

    _stub_series(monkeypatch)

    class _BrokenStore(_FakeStore):
        async def create(self, run):
            raise RuntimeError("store down")

    disp = D.RunDispatcher(runner=object(), store=_BrokenStore())
    with pytest.raises(RuntimeError):
        await disp.run_template(
            "demo/tmpl", inputs={}, series_id="s-broken", estimated_usd=50.0)
    assert (await reserved_total("s-broken")) == 0.0


async def test_close_reservations_settles_a_hold_taken_by_another_owner():
    """The resume path's primitive: close by RUN ID, without the Reservation
    object. Under the Postgres backend this is what lets replica B settle a
    claim replica A admitted (and what a restart-crossing HITL resume uses)."""
    from cairn.core.dispatch import dispatcher as D

    await reserve("s-hitl", est_usd=25.0, spent_usd=0.0, budget_usd=100.0,
                  run_id="run-hitl")
    await D.RunDispatcher._close_reservations("run-hitl", settled=True, actual_usd=20.0)
    assert (await reserved_total("s-hitl")) == 0.0


async def test_close_reservations_is_a_no_op_for_an_unknown_run():
    from cairn.core.dispatch import dispatcher as D

    await D.RunDispatcher._close_reservations("no-such-run", settled=False)


# ── live Postgres: the fleet-safety claims (#1530) ──────────────────────────
#
# Skip-gated on PGSTORE_DSN exactly like tests/core/test_account.py. Two
# separate asyncpg pools inside ONE process are an honest stand-in for two
# control-plane replicas — they are genuinely independent connections against
# one database, which is where the atomicity has to hold — but they are NOT the
# real two-process acceptance run. That one is a box-session step: start two
# control planes against one Postgres and submit concurrently. Everything below
# is written so it runs unchanged there.


@pytest.fixture
def _pg_dsn():
    dsn = os.environ.get("PGSTORE_DSN")
    if not dsn:
        pytest.skip("PGSTORE_DSN not set — skipping live Postgres integration")
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        pytest.skip("storage-postgres optional dep group not installed")
    return dsn


def _series(name: str) -> str:
    """A unique series per test — the Postgres backend deliberately does NOT
    truncate on reset (durable holds are the point), so tests must not share
    a series id with each other or with a live deployment."""
    import uuid

    return f"test-{name}-{uuid.uuid4().hex[:8]}"


async def _replicas(dsn):
    """Two backends on two INDEPENDENT pools — one per simulated replica."""
    import asyncpg

    pool_a = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
    pool_b = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
    return (B._PostgresBackend(pool=pool_a), B._PostgresBackend(pool=pool_b),
            pool_a, pool_b)


async def test_live_pg_two_replicas_and_a_budget_for_one(_pg_dsn):
    """THE #1530 acceptance shape: a series whose budget covers exactly one run,
    concurrent submissions from two replicas — exactly one admits, the other
    gets BudgetExceeded."""
    a, b, pa, pb = await _replicas(_pg_dsn)
    sid = _series("one-winner")
    try:
        out = await asyncio.gather(
            a.reserve(sid, amount=60.0, spent_usd=0.0, budget_usd=100.0, run_id="r-a"),
            b.reserve(sid, amount=60.0, spent_usd=0.0, budget_usd=100.0, run_id="r-b"),
            return_exceptions=True,
        )
        ok = [r for r in out if isinstance(r, Reservation)]
        refused = [r for r in out if isinstance(r, BudgetExceeded)]
        assert len(ok) == 1 and len(refused) == 1, out
    finally:
        await pa.close()
        await pb.close()


async def test_live_pg_many_concurrent_reserves_never_exceed_the_cap(_pg_dsn):
    """Ten $30 claims against a $100 cap, split across two replicas: exactly
    three win, and the shared total never exceeds the budget."""
    a, b, pa, pb = await _replicas(_pg_dsn)
    sid = _series("cap")
    try:
        out = await asyncio.gather(*[
            (a if i % 2 == 0 else b).reserve(
                sid, amount=30.0, spent_usd=0.0, budget_usd=100.0, run_id=f"r{i}")
            for i in range(10)
        ], return_exceptions=True)
        winners = [r for r in out if isinstance(r, Reservation)]
        assert len(winners) == 3, f"{len(winners)} winners — cap allows 3"
        assert (await a.reserved_total(sid)) <= 100.0
    finally:
        await pa.close()
        await pb.close()


async def test_live_pg_holds_survive_a_process_restart(_pg_dsn):
    """Restart safety: the hold is a row, so a FRESH pool (standing in for the
    process that comes back up) still sees it and still refuses the run the cap
    can no longer afford."""
    import asyncpg

    sid = _series("restart")
    run_id = f"r-restart-{sid[-8:]}"  # unique per run: a reused database keeps old holds (#2551)
    pool1 = await asyncpg.create_pool(_pg_dsn, min_size=1, max_size=2)
    try:
        await B._PostgresBackend(pool=pool1).reserve(
            sid, amount=90.0, spent_usd=0.0, budget_usd=100.0, run_id=run_id)
    finally:
        await pool1.close()  # the process dies with the claim still held

    pool2 = await asyncpg.create_pool(_pg_dsn, min_size=1, max_size=2)
    try:
        fresh = B._PostgresBackend(pool=pool2)
        assert (await fresh.reserved_total(sid)) == pytest.approx(90.0)
        with pytest.raises(BudgetExceeded):
            await fresh.reserve(sid, amount=90.0, spent_usd=0.0, budget_usd=100.0,
                                run_id="r-after")
        # …and it is still ATTRIBUTABLE, which is what a stale-hold reaper
        # (follow-up, see _STALE_HOLD_REAPER) would drive.
        assert [r.run_id for r in await fresh.held_for_run(run_id)] == [run_id]
    finally:
        await pool2.close()


async def test_live_pg_settle_and_release_are_idempotent_state_transitions(_pg_dsn):
    """held → settled/released, never a delete (the table is the audit trail);
    closing an already-terminal reservation updates nothing and does not raise."""
    import asyncpg

    sid = _series("idempotent")
    pool = await asyncpg.create_pool(_pg_dsn, min_size=1, max_size=2)
    try:
        be = B._PostgresBackend(pool=pool)
        r1 = await be.reserve(sid, amount=40.0, spent_usd=0.0, budget_usd=100.0,
                              run_id="r-settle")
        r2 = await be.reserve(sid, amount=40.0, spent_usd=0.0, budget_usd=100.0,
                              run_id="r-release")
        await be.close(r1, state="settled", actual_usd=33.0)
        await be.close(r1, state="settled", actual_usd=33.0)  # idempotent
        await be.close(r2, state="released")
        await be.close(r2, state="released")  # idempotent
        assert (await be.reserved_total(sid)) == 0.0
        async with pool.acquire() as c:
            rows = await c.fetch(
                "SELECT reservation_id, state, actual_usd::float8 AS actual_usd "
                "FROM series_reservations WHERE series_id = $1 ORDER BY created_at", sid)
        states = {r["reservation_id"]: r["state"] for r in rows}
        assert states == {r1.id: "settled", r2.id: "released"}, "rows must survive"
        assert next(r["actual_usd"] for r in rows if r["reservation_id"] == r1.id) == 33.0
    finally:
        await pool.close()
