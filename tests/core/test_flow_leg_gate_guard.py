"""#1379 — declared-behavior markers must survive `resolve_flow_ref`, and a
gate-carrying flow must never run WHOLE as a pipeline leg.

Two halves of one defect:

* ``resolve_flow_ref`` built each StageRef from stage/config/resources/
  checkpoint and dropped ``gate`` (and, once ADR-0090 landed, ``map``). The
  engine runner has NO gate handling whatsoever, so a dropped marker is not a
  degraded gate — it is no gate at all, executing every stage silently.
* The compiler already refuses a gated flow bound to a whole-flow door
  (``assert_runnable_whole``, #1079), but the pipeline leg path — the one that
  actually submits engine work — never called it.

Mitigant at the time of the fix: no shipped flow declares ``gate:``. That is
precisely why the guard belongs here before one does.
"""
from __future__ import annotations

import pytest

from cairn.core.dataflow.defs import FlowDef, resolve_flow_ref
from cairn.core.dataflow.gate_split import assert_runnable_whole


def _flow(**stage_kw) -> FlowDef:
    return FlowDef.model_validate({
        "name": "guarded",
        "kind": "data",
        "stages": [{"stage": "ingest.normalize"}, {"stage": "dedup.exact", **stage_kw}],
    })


# ── markers ride through resolution ─────────────────────────────────────────

def test_gate_marker_survives_resolution(monkeypatch):
    """A resolved spec must still SAY what the record said."""
    import cairn.core.dataflow.defs as defs_mod
    monkeypatch.setattr(defs_mod, "load_flow_defs",
                        lambda: {"p/guarded": _flow(gate="promote")})
    stages = resolve_flow_ref("p/guarded", {})
    assert stages[1].gate == "promote"


def test_map_block_survives_resolution(monkeypatch):
    """ADR-0090: dropping `map` collapses a declared fan-out into one
    un-mapped call — the record says shard, execution says don't."""
    import cairn.core.dataflow.defs as defs_mod
    monkeypatch.setattr(
        defs_mod, "load_flow_defs",
        lambda: {"p/mapped": _flow(map={"over": "shards", "max_parallel": 8})})
    stages = resolve_flow_ref("p/mapped", {})
    assert stages[1].map is not None
    assert stages[1].map.max_parallel == 8


def test_ungated_flow_resolves_unchanged(monkeypatch):
    """Regression guard: the ordinary path grows no new behavior."""
    import cairn.core.dataflow.defs as defs_mod
    monkeypatch.setattr(defs_mod, "load_flow_defs", lambda: {"p/plain": _flow()})
    stages = resolve_flow_ref("p/plain", {})
    assert [s.stage for s in stages] == ["ingest.normalize", "dedup.exact"]
    assert stages[1].gate is None and stages[1].map is None


# ── the guard the leg path now calls ────────────────────────────────────────

def test_guard_refuses_a_gated_flow_run_whole():
    with pytest.raises(Exception, match="declares in-flow approval gate"):
        assert_runnable_whole(_flow(gate="promote"), site="pipeline leg p/guarded")


def test_guard_names_the_site_so_the_error_locates_itself():
    with pytest.raises(Exception, match="pipeline leg p/guarded"):
        assert_runnable_whole(_flow(gate="promote"), site="pipeline leg p/guarded")


def test_guard_passes_an_ungated_flow():
    assert assert_runnable_whole(_flow(), site="pipeline leg p/plain") is None


# ── the leg path itself fails closed ────────────────────────────────────────

@pytest.mark.asyncio
async def test_gated_flow_leg_fails_instead_of_running_ungated(monkeypatch):
    """The end the user feels: a gated flow bound as a pipeline leg comes back
    as a FAILED leg naming the gate, never as a silently-ungated success.

    The engine is a tripwire — reaching it at all means the guard failed."""
    from cairn.core.dispatch import leg_executor as le

    monkeypatch.setattr("cairn.core.dataflow.defs.load_flow_defs",
                        lambda: {"p/guarded": _flow(gate="promote")})

    class _Tripwire:
        async def submit(self, *a, **k):  # pragma: no cover — guard must fire first
            raise AssertionError("a gated flow must not reach engine submission")

    outcome = await le.execute_leg(
        "flow:p/guarded", inputs={}, engine=_Tripwire(), run_id="run-1", kind="data")
    assert outcome.state == "failed"
    assert "approval gate" in (outcome.error or "")


@pytest.mark.asyncio
async def test_an_ungated_leg_is_unaffected_by_the_guard(monkeypatch):
    """Regression: the guard must not stand between ordinary legs and the
    engine — an ungated flow still reaches submit."""
    from cairn.core.dispatch import leg_executor as le

    monkeypatch.setattr("cairn.core.dataflow.defs.load_flow_defs",
                        lambda: {"p/plain": _flow()})
    reached: list[str] = []

    class _Recording:
        name = "stub"

        def validate(self, spec):
            reached.append("validate")
            return ["stop here — the guard already let us through"]

    outcome = await le.execute_leg(
        "flow:p/plain", inputs={}, engine=_Recording(), run_id="run-1", kind="data")
    assert reached == ["validate"], "an ungated leg must still reach the engine"
    assert outcome.state == "failed"          # stopped by validate, not the guard
    assert "approval gate" not in (outcome.error or "")
