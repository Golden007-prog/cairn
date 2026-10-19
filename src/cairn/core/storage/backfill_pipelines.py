"""History backfill: give every pipeline-less run the pipeline it belongs to
(#2128 item C, ADR-0105 D1/D2).

**Why this exists.** #2133 moved the no-orphan-runs invariant into the ONE
chokepoint every producer calls (``RunDispatcher.run_template``), so no new run
can be created without a pipeline. That closes the future; it cannot touch the
past. Rows written before the chokepoint landed still read
``bundle_pipeline_id = ''`` and the console badges them ``no pipeline``. The
owner's call (2026-09-09) is a **backfill on both local and prod**, not a
permanent ``legacy`` label: the rule is total or it is not a rule.

**There is no migration runner to hang this off.** The stores in this package
create their schema with ``CREATE TABLE IF NOT EXISTS`` in ``__init__`` and
evolve it with :func:`cairn.core.storage.migrations.add_missing_columns` /
``ADD COLUMN IF NOT EXISTS`` at connect time — a *schema* primitive with no
concept of a one-shot *data* migration, no version table, and no ordering. The
one thing it does do is run on every connect, which is exactly wrong for this:
a backfill that fires implicitly whenever a store opens would write pipeline
records on a prod boot with nobody watching, and could not be dry-run. So this
is an **idempotent, dry-run-by-default CLI command** (``cairn pipeline
backfill``) over both tiers instead. ADR-0050's central migration runner, when
it exists, can call :func:`backfill_orphan_runs` unchanged.

**The record has to be indistinguishable from a live one**, except by its
provenance — otherwise the console grows a second code path for history. The
shape below is copied from what
:meth:`cairn.core.dispatch.dispatcher.RunDispatcher._wrap_orphan_run` produces:
the same :func:`~cairn.core.dispatch.start.synthesize_flow_pipeline` definition,
the same ``plan_pipeline`` plan, the same ``flow:<template_ref>`` ``bundle_ref``
(ADR-0084 D0), one leg whose ``run_id`` is the run. What differs is that a
backfilled record is written *terminal*, mirroring a run that already finished,
and its ``trigger`` says ``{"source": "backfill", "backfilled_at": …}``.

**Reversible**, because of that trigger. The exact queries are
:data:`REVERSAL_SQL_SQLITE` / :data:`REVERSAL_SQL_POSTGRES`, and
:func:`revert_backfill` is the same predicate through the store API — it deletes
each record and clears ``bundle_pipeline_id``/``step_id`` on the run it names.
Nothing else in the store carries ``trigger.source == "backfill"``: the live
sources are ``start``/``launch``/``dispatch``/``schedule``/``webhook``/``cli``.

**A backfilled record never makes the control plane act.** Only TERMINAL runs
are wrapped. A record whose leg reads ``queued`` is re-submitted by
``PipelineBridge._adopt_in_flight`` on the next restart, so backfilling a zombie
``running``/``queued`` run would start work — possibly paid — for a run that
died months ago. Those are reported, not adopted: a zombie is a different
decision from history. See :data:`_STATUS_MAP`.

**Report, never invent.** A run whose ``template_ref`` is empty has no flow
address to make a leg of, so it is NAMED in the report rather than given a
guessed ref — a false address in the audit trail is worse than a run the report
counts. ``--infer-ref`` is the opt-in escape hatch, and it is not a guess
either: it asks :func:`cairn.core.eval.scoring.run_template_ref`, THE canonical
resolver every surface already delegates to, whose legacy chain exists for
exactly these pre-#1096 rows. It is off by default because the answer is
second-hand, and a record built from it is marked ``trigger.ref_inferred``. On
the local dev store this is the difference between the invariant stopping at 36
and reaching 0 — see the PR.

**Idempotent and resumable.** The scan predicate is "the run has no
``bundle_pipeline_id``", which is exactly what a successful wrap clears, so a
second pass finds nothing and a pass interrupted half way resumes where it
stopped — no cursor file, no marker table. Paging is keyset by run ``id``
(the primary key of both tiers), never ``OFFSET``, so a batch that skips an
unbackfillable run cannot loop on it forever and a large prod table never lands
in memory.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from cairn.core._time import utc_now_iso
from cairn.core.storage.pipeline_store import PipelineRecord, StepState
from cairn.core.storage.protocols import Run

logger = logging.getLogger(__name__)

#: The ``trigger.source`` every backfilled record carries. The one value that
#: makes the write auditable and reversible; nothing on the live path uses it.
BACKFILL_SOURCE = "backfill"

#: ``flow:`` — a run whose ``template_ref`` already carries it is a data/ml leg
#: (ADR-0084 D2), so its one-leg pipeline must use the ``flow:`` leg shape too.
#: Spelled here rather than imported from ``dispatch.leg_executor`` to keep the
#: storage layer off the dispatch import path.
FLOW_PREFIX = "flow:"

#: How many runs one scan batch pulls. Deliberately modest: the batch is
#: hydrated into pydantic models, and prod's `runs` table is the biggest in the
#: database.
DEFAULT_BATCH_SIZE = 200

#: Terminal run status → (step_state status, pipeline status). Mirrors what the
#: bridge writes: :meth:`PipelineBridge._handle_terminal` sets the step ``done``
#: for a ``run.done`` terminal and ``failed`` for anything else, and
#: ``_recompute_status`` / ``_fail_pipeline`` derive the record's own status
#: from that.
#:
#: **Only terminal runs are here, and that is a safety rule, not an omission.**
#: A backfilled record must never make the control plane DO anything, and the
#: resume machinery acts on exactly the states this map would otherwise
#: produce: `plan_resume`'s ``_RESUBMIT`` bucket is ``("pending", "queued")``,
#: and ``PipelineBridge._adopt_in_flight`` hands a ``queued`` step with a
#: ``run_id`` to ``_resume_queued_leg``, which re-arms the compute-target wait —
#: i.e. a backfilled `queued` leg would START (possibly paid) work for a run
#: that died months ago, on the next control-plane restart. A ``running`` step
#: is milder but not free: with no ``handle`` it lands in ``resume.orphaned``
#: and the ADR-0086 H1 reconciler judges it.
#:
#: So a non-terminal pipeline-less run is REPORTED, like a run with no ref. It
#: is a zombie from a dead control plane, not history, and adopting it is a
#: different decision from recording what already happened. Every orphan in
#: every local store is terminal; prod may differ, which is what the dry run is
#: for.
_STATUS_MAP: dict[str, tuple[str, str]] = {
    "done": ("done", "done"),
    "failed": ("failed", "failed"),
    "cancelled": ("failed", "failed"),
    "expired": ("failed", "failed"),
}

#: The reversal, as SQL, for an operator who would rather see the rows than
#: trust a command. Both list; swap ``SELECT …`` for ``DELETE`` to remove.
REVERSAL_SQL_SQLITE = (
    "SELECT pipeline_id, bundle_ref, status FROM pipelines "
    "WHERE json_extract(data, '$.trigger.source') = 'backfill'"
)
REVERSAL_SQL_POSTGRES = (
    "SELECT pipeline_id, bundle_ref, status FROM pipelines "
    "WHERE data->'trigger'->>'source' = 'backfill'"
)

#: The invariant's own count, per tier — `bundle_pipeline_id` rides the JSON
#: `data` blob (it is neither filtered nor indexed on), so #2128 §6 criterion 3
#: is spelled against the blob, not against a column that does not exist.
ORPHAN_COUNT_SQL_SQLITE = (
    "SELECT count(*) FROM runs "
    "WHERE COALESCE(json_extract(data, '$.bundle_pipeline_id'), '') = ''"
)
ORPHAN_COUNT_SQL_POSTGRES = (
    "SELECT count(*) FROM runs "
    "WHERE COALESCE(data->>'bundle_pipeline_id', '') = ''"
)


class UnbackfillableRun(ValueError):
    """This run cannot be given a one-leg pipeline, and no ref may be invented.

    The only cause in practice is an empty ``template_ref`` — a legacy row, or a
    skill run that never resolved a template (see :class:`Run`'s own docstring).
    A pipeline leg's ref IS the flow's ADR-0084 D0 address, so there is nothing
    truthful to put there; guessing one would put a false address in the audit
    trail, which is worse than a run the report names.
    """


@dataclass
class BackfillReport:
    """What one pass saw and did. ``applied`` False = nothing was written."""

    applied: bool = False
    scanned: int = 0
    wrapped: int = 0
    #: run_id → reason, for every run the pass could not synthesize.
    skipped: dict[str, str] = field(default_factory=dict)
    #: (run_id, pipeline_id) for each record written (or that would be).
    pairs: list[tuple[str, str]] = field(default_factory=list)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    def summary(self) -> str:
        verb = "wrapped" if self.applied else "would wrap"
        return (f"scanned {self.scanned}, {verb} {self.wrapped}, "
                f"skipped {self.skipped_count}")


@dataclass
class RevertReport:
    """What one reversal pass saw and did."""

    applied: bool = False
    found: int = 0
    removed: int = 0
    runs_cleared: int = 0
    pipeline_ids: list[str] = field(default_factory=list)

    def summary(self) -> str:
        verb = "removed" if self.applied else "would remove"
        return (f"found {self.found} backfilled record(s), {verb} {self.removed}, "
                f"cleared {self.runs_cleared} run(s)")


def flow_ref_and_kind(template_ref: str) -> tuple[str, str]:
    """Split a run's ``template_ref`` into the flow address and its leg kind.

    ``"serving/batch-infer"`` → agentic (a bare template leg);
    ``"flow:serving/batch-infer"`` → data (a ``flow:`` leg, ADR-0084 D2). The
    prefix is how a data/ml leg was already recorded on the run, so this reads
    the kind off the history rather than guessing it from a registry that may
    have been reinstalled since.
    """
    ref = (template_ref or "").strip()
    if ref.startswith(FLOW_PREFIX):
        return ref[len(FLOW_PREFIX):], "data"
    return ref, "agentic"


def resolve_template_ref(run: Run, *, infer: bool = False) -> tuple[str, str]:
    """``(ref, source)`` for this run — ``("", "")`` when nothing truthful applies.

    ``source`` is ``"recorded"`` for :attr:`Run.template_ref` (the authoritative
    field, #1096) and ``"inferred"`` for the legacy chain.

    **Why ``infer`` is opt-in and OFF by default.** The backfill's rule is
    report, never invent: a run with no ``template_ref`` gets named, not given a
    guessed address. But "no ``template_ref``" is not the same as "unknowable" —
    :func:`cairn.core.eval.scoring.run_template_ref` is THE canonical resolver
    every surface already delegates to (dispatcher, ``triggers.server``, the
    console), and for a row written before #1096 it reads the ref off
    ``results[].template_name``, the trigger payload, or a path-like
    fingerprint. That is the codebase's own answer to "what did this run
    execute", not a guess — and it is what the console shows for these rows
    TODAY. Measured on the local dev store, it resolves all 36 of the 53
    pipeline-less runs that carry no ``template_ref``.

    It is still second-hand, so it is a flag rather than the default, and a
    record built from it says so (``trigger.ref_inferred``) — the operator
    decides whether the invariant reaching 0 is worth a ref that was read off a
    fingerprint rather than stamped at admission.
    """
    recorded = (run.template_ref or "").strip()
    if recorded:
        return recorded, "recorded"
    if not infer:
        return "", ""
    # lazy: eval.scoring pulls the eval ledger, which imports storage — a
    # module-level import here is a cycle.
    from cairn.core.eval.scoring import run_template_ref  # noqa: PLC0415

    return (run_template_ref(run) or "").strip(), "inferred"


def synthesize_backfill_record(run: Run, *, now: Optional[str] = None,
                               infer_ref: bool = False) -> PipelineRecord:
    """The one-leg :class:`PipelineRecord` this already-finished run belongs to.

    Pure: builds and returns, writes nothing. The shape is
    ``_wrap_orphan_run``'s, settled to the run's terminal state:

    * ``bundle_ref = flow:<template_ref>`` — ADR-0084 D0, the same stamp the
      chokepoint and both doors use for a synthesized definition.
    * one leg, the flow itself, from
      :func:`~cairn.core.dispatch.start.synthesize_flow_pipeline` and
      :func:`~cairn.core.engine.pipeline.plan_pipeline` — the SAME functions the
      live path calls, so the plan cannot drift from what the console renders.
    * ``step_state`` holds one :class:`StepState` whose ``run_id`` is this run,
      with the status the bridge would have written for its terminal, the run's
      ``final_verdict`` as the verdict, and the run's own timestamps.
    * ``created_at``/``ended_at``/``workspace_id``/``environment``/``inputs``
      from the run, so the record sits in the console's history where the run
      does rather than at today's date.
    * ``trigger.source = "backfill"`` — the provenance that makes it auditable
      and reversible, plus the original run's trigger source so the record still
      answers "what started this" for the run underneath.

    ``infer_ref`` hands the ref question to :func:`resolve_template_ref`'s
    opt-in legacy chain; a record built that way is marked
    ``trigger.ref_inferred``. Raises :class:`UnbackfillableRun` when there is
    no truthful ref to use.
    """
    # lazy: engine + dispatch import each other's packages and the compiler; a
    # module-level import here would put both on `cairn --help`'s path and make
    # storage → dispatch → storage a cycle. Same reason `_wrap_orphan_run` does
    # it lazily.
    from cairn.core.dispatch.start import synthesize_flow_pipeline  # noqa: PLC0415
    from cairn.core.engine.pipeline import plan_pipeline  # noqa: PLC0415

    if str(run.status) not in _STATUS_MAP:
        raise UnbackfillableRun(
            f"run {run.id} is {run.status}, not terminal — a backfilled record for a "
            f"live-looking leg would be ACTED ON by the bridge on the next restart "
            f"(a `queued` leg is re-submitted), so a zombie run is reported rather "
            f"than adopted (#2128 C)"
        )
    raw, ref_source = resolve_template_ref(run, infer=infer_ref)
    ref, kind = flow_ref_and_kind(raw)
    if not ref:
        raise UnbackfillableRun(
            f"run {run.id} has no template_ref — there is no flow address to make "
            f"a leg of, and inventing one would write a false ref into the audit "
            f"trail (#2128 C)"
            + ("" if infer_ref else "; --infer-ref asks the canonical resolver")
        )
    try:
        d = synthesize_flow_pipeline(ref, typed=dict(run.inputs or {}), kind=kind)
        plan = plan_pipeline(d)
    except Exception as e:  # noqa: BLE001 — a ref that is not <pack>/<name>
        raise UnbackfillableRun(f"run {run.id}: {ref!r} cannot be a one-leg pipeline ({e})") from e

    step_id = d.steps[0].id
    step_status, pipeline_status = _STATUS_MAP[str(run.status)]
    stamp = now or utc_now_iso()

    return PipelineRecord(
        bundle_ref=f"{FLOW_PREFIX}{ref}",
        pipeline_name=d.name,
        status=pipeline_status,
        plan=plan,
        inputs=dict(run.inputs or {}),
        environment=run.environment or None,
        workspace_id=run.workspace_id,
        step_state={step_id: StepState(
            run_id=run.id,
            status=step_status,
            verdict=run.final_verdict or None,
            started_at=run.started_at or run.created_at,
            ended_at=run.ended_at,
        )},
        # a settled step is off the cursor, as the bridge leaves it — and every
        # backfilled record is settled by construction (see _STATUS_MAP).
        cursor=[],
        trigger={
            "source": BACKFILL_SOURCE,
            "backfilled_at": stamp,
            "issue": "2128",
            "flow": ref,
            "one_leg": True,
            "run_id": run.id,
            # second-hand ref (`--infer-ref`) is marked, so a reader can tell a
            # record built on an admission-stamped address from one built on a
            # legacy inference — and can find them all with one predicate.
            **({"ref_inferred": True} if ref_source == "inferred" else {}),
            # what started the RUN, kept so the record can still answer it.
            "original_trigger": {
                "source": getattr(run.trigger, "source", "") or "",
                "service": getattr(run.trigger, "service", None),
            },
            "mode": "sandbox" if run.sandbox else "live",
        },
        created_at=run.created_at,
        ended_at=run.ended_at,
    )


async def backfill_orphan_runs(
    *,
    store: Any,
    pipeline_store: Any,
    apply: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    workspace_id: Optional[str] = None,
    limit: int = 0,
    infer_ref: bool = False,
) -> BackfillReport:
    """Give every pipeline-less run in ``store`` its one-leg pipeline.

    ``apply=False`` (the default) is a **dry run**: it scans, synthesizes each
    record in memory so a run that cannot be backfilled is reported by the same
    code that would have written it, and returns the counts without touching
    either store. A prod operator sees the number before anything moves.

    ``limit`` caps how many runs are scanned (0 = all) — for a first look at a
    large table.

    Resumability comes from the predicate, not from bookkeeping: a wrapped run
    stops matching "no ``bundle_pipeline_id``", so re-running continues where a
    killed pass stopped and a completed pass is a no-op. Within one pass the
    paging is keyset by run id, so a run this pass SKIPS (no ``template_ref``)
    is stepped over rather than re-fetched forever.

    The two writes are ordered record-then-run. A crash between them leaves a
    pipeline record whose run still reads orphaned — the next pass re-wraps that
    run and the abandoned record is one `trigger.source == "backfill"` row that
    :func:`revert_backfill` removes. The other order would leave a run pointing
    at a pipeline that does not exist, which the console renders as a broken
    link.
    """
    report = BackfillReport(applied=apply)
    after_id = ""
    while True:
        take = batch_size
        if limit:
            remaining = limit - report.scanned
            if remaining <= 0:
                break
            take = min(batch_size, remaining)
        batch = await store.list_pipeline_less_runs(
            after_id=after_id, limit=take, workspace_id=workspace_id
        )
        if not batch:
            break
        after_id = batch[-1].id
        for run in batch:
            report.scanned += 1
            try:
                rec = synthesize_backfill_record(run, infer_ref=infer_ref)
            except UnbackfillableRun as e:
                # debug per run, ONE warning at the end: a store can hold
                # hundreds of these (36 of 53 orphans in the local dev store),
                # and a line each buries the summary the operator came for.
                report.skipped[run.id] = str(e)
                logger.debug("backfill skipped %s: %s", run.id, e)
                continue
            report.wrapped += 1
            report.pairs.append((run.id, rec.pipeline_id))
            if not apply:
                continue
            step_id = next(iter(rec.step_state))
            await pipeline_store.put(rec)
            await store.update(run.id, bundle_pipeline_id=rec.pipeline_id, step_id=step_id)
            logger.info("backfilled run %s into pipeline %s (%s)",
                        run.id, rec.pipeline_id, rec.bundle_ref)
        if len(batch) < take:
            break
    if report.skipped:
        # The rows that keep the invariant's count above 0, named once so a
        # programmatic caller (a future ADR-0050 migration runner) sees them
        # without the CLI's report object.
        logger.warning(
            "backfill: %d of %d pipeline-less run(s) cannot be backfilled (no "
            "template_ref — no flow address to make a leg of): %s",
            report.skipped_count, report.scanned, sorted(report.skipped))
    return report


async def list_backfilled(pipeline_store: Any, *, batch_size: int = DEFAULT_BATCH_SIZE,
                          ) -> list[PipelineRecord]:
    """Every record this backfill wrote — the store-API spelling of
    :data:`REVERSAL_SQL_SQLITE` / :data:`REVERSAL_SQL_POSTGRES`."""
    out: list[PipelineRecord] = []
    after = ""
    while True:
        page = await pipeline_store.list_by_trigger_source(
            BACKFILL_SOURCE, after_id=after, limit=batch_size
        )
        if not page:
            break
        out.extend(page)
        after = page[-1].pipeline_id
        if len(page) < batch_size:
            break
    return out


async def revert_backfill(
    *,
    store: Any,
    pipeline_store: Any,
    apply: bool = False,
) -> RevertReport:
    """Undo the backfill: delete every ``trigger.source == "backfill"`` record and
    clear the ``bundle_pipeline_id``/``step_id`` it stamped on its run.

    Dry-run by default, like the forward pass. The run is found through the
    record's own ``trigger.run_id`` (not by scanning the runs table), so a
    reversal costs one read per record and cannot clear a run some OTHER
    pipeline owns. Order is run-then-record, the mirror of the forward pass:
    a crash between them leaves an orphan run and a stale backfilled record,
    which the next reversal (or the next backfill) settles.
    """
    report = RevertReport(applied=apply)
    for rec in await list_backfilled(pipeline_store):
        report.found += 1
        report.pipeline_ids.append(rec.pipeline_id)
        run_id = str((rec.trigger or {}).get("run_id") or "")
        if not apply:
            continue
        if run_id:
            try:
                run = await store.get(run_id)
            except Exception:  # noqa: BLE001 — a missing run must not stop the sweep
                run = None
            if run is not None and run.bundle_pipeline_id == rec.pipeline_id:
                await store.update(run_id, bundle_pipeline_id="", step_id="")
                report.runs_cleared += 1
        if await pipeline_store.delete(rec.pipeline_id):
            report.removed += 1
    return report


__all__ = [
    "BACKFILL_SOURCE",
    "BackfillReport",
    "ORPHAN_COUNT_SQL_POSTGRES",
    "ORPHAN_COUNT_SQL_SQLITE",
    "REVERSAL_SQL_POSTGRES",
    "REVERSAL_SQL_SQLITE",
    "RevertReport",
    "UnbackfillableRun",
    "backfill_orphan_runs",
    "flow_ref_and_kind",
    "list_backfilled",
    "resolve_template_ref",
    "revert_backfill",
    "synthesize_backfill_record",
]
