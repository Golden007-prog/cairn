"""ADR-0103 (#2035) — the operator reporting contract, and every gate proven able to fail.

The contract has four halves and each has a test that goes red when it is
removed:

1. the declaration is REQUIRED on the class (``derive_entry`` refuses a
   missing one; no backfill exemption remains, #2415) and mirrored in the manifest
   (``entry_drift`` flags a mismatch; the rendered table round-trips);
2. the typed reporter refuses an undeclared name in strict mode and drops it
   with ONE warning otherwise, never raising in a job;
3. the runtime records every step's effective ``parameters`` before ``run``,
   with each config field's scope, and never lets a credential-shaped key through;
4. a declaration cannot be decorative — the conformance helper fails an
   operator that declares a series it never emits.
"""
from __future__ import annotations

import asyncio
import logging
import tomllib

import pytest
from pydantic import BaseModel, Field

from cairn.core.operator.manifest_sync import (
    derive_entry,
    entry_drift,
    render_operator_table,
)
from cairn.core.operator.protocol import Operation
from cairn.core.operator.reports import (
    METRICS_EVENT_TYPE,
    PARAMETERS_EVENT_TYPE,
    PROGRESS_EVENT_TYPE,
    SCALARS_EVENT_TYPE,
    ListSink,
    Reporter,
    ReportSpec,
    Series,
    UndeclaredReportError,
)
from cairn.core.operator.testing import assert_reports_conform, run_operator_for_test
from cairn.core.pack.manifest import load_manifest


class _In(BaseModel):
    rows: int = 3
    api_key: str = "sk-live-secret"


class _Cfg(BaseModel):
    budget: float = Field(1.0, json_schema_extra={"scope": "operational"})
    allow_stub: bool = Field(False, json_schema_extra={"scope": "governance"})


class _Out(BaseModel):
    done: int = 0


class _ReportingOp(Operation[_In, _Out]):
    name = "t2035/reporting"
    version = "0.1.0"
    input_schema = _In
    output_schema = _Out
    config_schema = _Cfg
    reports = ReportSpec(
        stages=["load", "work"],
        series=[Series("loss", "nats"), ("rows_per_s", "rows/s")],
        scalars=["final_loss"],
    )

    def run(self, inp, ctx):
        ctx.report.stage("load", percent=0)
        for step in range(inp.rows):
            ctx.report.series(step, loss=1.0 / (step + 1), rows_per_s=10.0)
        ctx.report.stage("work", percent=100)
        ctx.report.scalars(final_loss=0.1)
        return _Out(done=inp.rows)


class _DecorativeOp(_ReportingOp):
    """Declares a series it never emits — the conformance gate must catch it."""

    name = "t2035/decorative"
    reports = ReportSpec(
        stages=["load", "work"],
        series=[Series("loss", "nats"), ("rows_per_s", "rows/s"), Series("never_emitted", "")],
        scalars=["final_loss"],
    )


class _SilentOp(Operation[_In, _Out]):
    name = "t2035/silent"
    version = "0.1.0"
    input_schema = _In
    output_schema = _Out
    reports = ReportSpec.NONE

    def run(self, inp, ctx):
        return _Out(done=inp.rows)


class _UndeclaredOp(Operation[_In, _Out]):
    """Predates the rule: no ``reports`` attribute at all."""

    name = "t2035/undeclared"
    version = "0.1.0"
    input_schema = _In
    output_schema = _Out

    def run(self, inp, ctx):
        return _Out()


# ─── 1. the declaration ───────────────────────────────────────────────────


def test_report_spec_validates_names_and_rejects_duplicates():
    with pytest.raises(ValueError, match="invalid name"):
        ReportSpec(series=["Loss"])
    with pytest.raises(ValueError, match="duplicate"):
        ReportSpec(scalars=["a", "a"])
    assert ReportSpec.NONE.is_none and not _ReportingOp.reports.is_none
    assert ReportSpec.from_manifest(_ReportingOp.reports.to_manifest()) == _ReportingOp.reports


def test_missing_declaration_fails_derive_entry():
    with pytest.raises(ValueError, match="no `reports` declaration"):
        derive_entry(_UndeclaredOp)


def test_explicit_none_and_absent_table_are_the_same_declaration():
    e = derive_entry(_SilentOp)
    assert e.reports is None
    assert "reports" not in render_operator_table(_SilentOp)
    assert entry_drift(e, derive_entry(_SilentOp)) == []


def test_rendered_reports_table_round_trips_and_drift_catches_a_mismatch():
    text = render_operator_table(_ReportingOp)
    tbl = tomllib.loads(text)["operator"]["t2035/reporting"]["reports"]
    assert tbl["stages"] == ["load", "work"]
    assert tbl["series"] == [{"name": "loss", "unit": "nats"}, {"name": "rows_per_s", "unit": "rows/s"}]
    assert tbl["scalars"] == ["final_loss"]
    fresh = derive_entry(_ReportingOp)
    assert entry_drift(fresh, derive_entry(_ReportingOp)) == []

    class Renamed(_ReportingOp):
        reports = ReportSpec(stages=["load", "work"], series=[Series("train_loss", "nats"), ("rows_per_s", "rows/s")], scalars=["final_loss"])

    diffs = entry_drift(fresh, derive_entry(Renamed))
    assert diffs and diffs[0].startswith("reports:")


def test_manifest_parser_reads_and_validates_the_reports_table(tmp_path):
    (tmp_path / "pack.toml").write_text(
        '[pack]\nname = "t2035"\nversion = "0.1.0"\nkind = "operator"\n\n'
        '[operator."t2035/reporting"]\nversion = "0.1.0"\nrisk = "read_only"\nexecutors = ["in_process"]\n\n'
        '[operator."t2035/reporting".reports]\nstages = ["load"]\n'
        'series = [{ name = "loss", unit = "nats" }]\nscalars = ["final_loss"]\n'
    )
    m = load_manifest(tmp_path / "pack.toml")
    spec = m.operators["t2035/reporting"].reports
    assert spec.stages == ("load",) and spec.series == (Series("loss", "nats"),) and spec.scalars == ("final_loss",)

    (tmp_path / "pack.toml").write_text(
        '[pack]\nname = "t2035"\nversion = "0.1.0"\nkind = "operator"\n\n'
        '[operator."t2035/bad"]\nversion = "0.1.0"\nrisk = "read_only"\nexecutors = ["in_process"]\n\n'
        '[operator."t2035/bad".reports]\nseriez = ["loss"]\n'
    )
    with pytest.raises(Exception, match="unknown key"):
        load_manifest(tmp_path / "pack.toml")


# ─── 2. the typed reporter ────────────────────────────────────────────────


def test_strict_reporter_raises_on_an_undeclared_name():
    r = Reporter(spec=_ReportingOp.reports, node="n", sink=ListSink(), strict=True)
    with pytest.raises(UndeclaredReportError, match="stage 'unpack'"):
        r.stage("unpack")
    with pytest.raises(UndeclaredReportError, match="series 'accuracy'"):
        r.series(1, accuracy=0.5)
    with pytest.raises(UndeclaredReportError, match="scalar 'wat'"):
        r.scalars(wat=1)


def test_lenient_reporter_drops_with_one_warning_per_name_and_never_raises(caplog):
    events: list = []
    r = Reporter(spec=_ReportingOp.reports, node="n", sink=ListSink(events), strict=False)
    with caplog.at_level(logging.WARNING, logger="cairn.core.operator.reports"):
        r.series(1, loss=0.5, accuracy=0.9)   # accuracy undeclared → dropped, loss kept
        r.series(2, accuracy=0.8)             # nothing declared → no event
        r.stage("unpack")
    assert [t for t, _ in events] == [METRICS_EVENT_TYPE]
    assert events[0][1]["series"] == {"loss": 0.5}
    assert sum("accuracy" in rec.message for rec in caplog.records) == 1  # warned once


def test_every_event_carries_node_and_a_monotonic_seq():
    events: list = []
    r = Reporter(spec=_ReportingOp.reports, node="trainer", sink=ListSink(events))
    r.stage("load", percent=0)
    r.series(1, loss=1.0)
    r.checkpoint("s3://x/ckpt-1", step=1, metric=1.0)
    r.divergence("nan", step=2)
    r.scalars(final_loss=0.9)
    assert [t for t, _ in events] == [PROGRESS_EVENT_TYPE, METRICS_EVENT_TYPE, "checkpoint", "divergence", SCALARS_EVENT_TYPE]
    assert [p["seq"] for _, p in events] == [1, 2, 3, 4, 5]
    assert all(p["node"] == "trainer" for _, p in events)


def test_a_sink_that_raises_never_reaches_the_operator():
    class Boom:
        def emit(self, *_a):
            raise RuntimeError("sink down")

    r = Reporter(spec=_ReportingOp.reports, node="n", sink=Boom())
    r.series(1, loss=0.1)  # must not raise
    assert r.seq == 1


# ─── 3. the runtime records parameters for every step ────────────────────


def test_runtime_records_effective_parameters_with_scopes_and_redaction():
    events: list = []
    out = asyncio.run(run_operator_for_test(
        _ReportingOp, {"rows": 2, "api_key": "sk-live-secret"},
        config={"budget": 5.0, "allow_stub": True},   # profile/env layer
        run_config={"budget": 9.0},                     # run layer wins for operational
        report_sink=events,
    ))
    assert out.done == 2
    params = [p for t, p in events if t == PARAMETERS_EVENT_TYPE]
    assert len(params) == 1, "exactly one parameters record per step"
    p = params[0]
    assert p["operator"] == "t2035/reporting" and p["seq"] == 1
    assert p["inputs"]["rows"] == 2
    assert p["inputs"]["api_key"] == "***", "credential-shaped input keys are redacted"
    assert p["config"]["budget"] == 9.0
    assert p["scopes"] == {"budget": "run", "allow_stub": "environment"}


def test_parameters_are_recorded_even_for_an_operator_that_reports_nothing():
    events: list = []
    asyncio.run(run_operator_for_test(_SilentOp, {"rows": 1}, report_sink=events))
    assert [t for t, _ in events] == [PARAMETERS_EVENT_TYPE]
    assert events[0][1]["scopes"] == {}


def test_harness_runs_the_reporter_strict_so_a_pack_test_catches_a_typo():
    class Typo(_ReportingOp):
        name = "t2035/typo"

        def run(self, inp, ctx):
            ctx.report.series(1, los=0.1)
            return _Out()

    with pytest.raises(UndeclaredReportError):
        asyncio.run(run_operator_for_test(Typo, {"rows": 1}))


# ─── 4. a declaration cannot be decorative ───────────────────────────────


def test_conformance_passes_a_faithful_operator_and_fails_a_decorative_one():
    events = asyncio.run(assert_reports_conform(_ReportingOp, {"rows": 2}))
    assert PROGRESS_EVENT_TYPE in [t for t, _ in events]
    with pytest.raises(AssertionError, match="never emitted: \\['never_emitted'\\]"):
        asyncio.run(assert_reports_conform(_DecorativeOp, {"rows": 2}))


# ─── the sink always appends on the STORE's loop (seen live 2026-09-06) ──────


def test_storage_sink_hands_appends_to_the_owning_loop_from_another_loop():
    """An operator body running under its own loop in a worker thread must not
    append on that loop: the store's asyncio.Lock is not thread-safe and two
    loops allocated the same seq. The sink routes to the loop it was built on."""
    import asyncio
    import threading

    from cairn.core.operator.reports import StorageSink

    seen: list[tuple[str, str]] = []
    owner = asyncio.new_event_loop()
    t = threading.Thread(target=owner.run_forever, daemon=True)
    t.start()
    try:
        async def append(rid, typ, payload):
            seen.append((threading.current_thread().name, typ))

        sink = StorageSink(append, "run-1", owner)

        def worker():
            async def body():
                sink.emit("metrics", {"seq": 1})
                await asyncio.sleep(0.05)
            asyncio.run(body())

        w = threading.Thread(target=worker, name="worker-loop")
        w.start()
        w.join(timeout=5)
        import time
        for _ in range(50):
            if seen:
                break
            time.sleep(0.02)
        assert seen and seen[0][1] == "metrics"
        assert seen[0][0] != "worker-loop"      # delivered on the owner's thread, not the worker's
    finally:
        owner.call_soon_threadsafe(owner.stop)
        t.join(timeout=2)
