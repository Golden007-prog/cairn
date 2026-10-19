"""In-flow HITL by SPLITTING the flow, not by engine-native pause (#1079).

ADR-0083: a gate inside a flow becomes ``flow A → cairn gate → flow B`` — two
engine jobs with an artifact handoff, the EXISTING approval queue between them.

Why not `supports_pause` per engine (the original #1079 shape): builtin and
Metaflow have no suspend primitive at all, so that design lands on a
one-engine capability while claiming to be a protocol capability. Splitting works on
every engine, adds ZERO protocol surface, and satisfies #1079's hard
requirement — one approval model — by construction rather than by per-engine
wiring.

The governance property under test: a `gate:` marker must NEVER be silently
dropped. A flow carrying one that is run whole is a gate that does not gate.
"""
from __future__ import annotations

import pytest

from cairn.core.dataflow.contract import StageRef
from cairn.core.dataflow.defs import FlowDef
from cairn.core.dataflow.gate_split import (
    FlowSegment,
    GateSplitError,
    handoff_config_key,
    has_in_flow_gates,
    split_at_gates,
)


def _flow(stages, handoff=True, **kw) -> FlowDef:
    d = {"name": "f", "stages": stages}
    if handoff:
        d["handoff"] = {"export": "export.jsonl", "ingest": "ingest.normalize"}
    d.update(kw)
    return FlowDef(**d)


def _s(stage, gate=None, **config) -> dict:
    out = {"stage": stage}
    if gate:
        out["gate"] = gate
    if config:
        out["config"] = config
    return out


# ── the no-gate path stays byte-identical ────────────────────────────────────

def test_a_flow_without_gates_yields_one_untouched_segment():
    flow = _flow([_s("ingest.normalize"), _s("dedup.exact")], handoff=False)
    segs = split_at_gates(flow)
    assert len(segs) == 1
    assert [s.stage for s in segs[0].stages] == ["ingest.normalize", "dedup.exact"]
    assert segs[0].gate_after is None
    assert has_in_flow_gates(flow) is False


def test_no_handoff_stages_are_injected_when_there_is_no_gate():
    """Every shipped flow today has no gates — none may gain a stage."""
    flow = _flow([_s("a"), _s("b")], handoff=False)
    assert len(split_at_gates(flow)[0].stages) == 2


# ── splitting ────────────────────────────────────────────────────────────────

def test_one_gate_splits_into_two_segments_with_handoff_injected():
    flow = _flow([
        _s("ingest.normalize"),
        _s("dedup.exact"),
        _s("pii.redact", gate="approve-redaction"),
        _s("export.jsonl"),
    ])
    a, b = split_at_gates(flow)

    # A ends with the injected export; B starts with the injected ingest.
    assert [s.stage for s in a.stages] == [
        "ingest.normalize", "dedup.exact", "export.jsonl"]
    assert [s.stage for s in b.stages] == [
        "ingest.normalize", "pii.redact", "export.jsonl"]
    assert a.gate_after == "approve-redaction"
    assert b.gate_after is None


def test_handoff_stages_point_at_the_same_path_placeholder():
    """A's export and B's ingest MUST agree, or the split silently loses data."""
    flow = _flow([_s("a"), _s("b", gate="g")])
    a, b = split_at_gates(flow)
    key = handoff_config_key(1)
    assert a.stages[-1].config["path"] == f"$config.{key}"
    assert b.stages[0].config["path"] == f"$config.{key}"
    assert a.handoff_key == key and b.resumes_from == key


def test_two_gates_yield_three_segments_with_distinct_handoffs():
    flow = _flow([_s("a"), _s("b", gate="g1"), _s("c", gate="g2"), _s("d")])
    segs = split_at_gates(flow)
    assert len(segs) == 3
    assert [s.gate_after for s in segs] == ["g1", "g2", None]
    keys = [s.handoff_key for s in segs[:-1]]
    assert len(set(keys)) == 2, "each handoff needs its own path, else B overwrites A"


def test_segment_names_are_stable_and_derived_from_the_flow():
    segs = split_at_gates(_flow([_s("a"), _s("b", gate="g")]))
    assert [s.name for s in segs] == ["f@1", "f@2"]


def test_original_stage_config_survives_the_split():
    flow = _flow([_s("a"), _s("b", gate="g", threshold=7)])
    _, b = split_at_gates(flow)
    redact = next(s for s in b.stages if s.stage == "b")
    assert redact.config["threshold"] == 7


# ── validation: the ways an author can get this wrong ────────────────────────

def test_a_gate_on_the_FIRST_stage_is_rejected():
    """Nothing runs before it — that is the BINDING's gate, already supported.
    Two ways to say the same thing is how one of them rots."""
    with pytest.raises(GateSplitError, match="(?i)first stage"):
        split_at_gates(_flow([_s("a", gate="g"), _s("b")]))


def test_gates_without_a_declared_handoff_are_rejected():
    """Segments run as SEPARATE engine jobs with separate workdirs — without a
    handoff the working dataset silently vanishes between them."""
    with pytest.raises(GateSplitError, match="handoff"):
        split_at_gates(_flow([_s("a"), _s("b", gate="g")], handoff=False))


def test_the_error_names_the_gate_and_the_fix():
    with pytest.raises(GateSplitError) as e:
        split_at_gates(_flow([_s("a"), _s("b", gate="approve-me")], handoff=False))
    msg = str(e.value)
    assert "approve-me" in msg and "export" in msg and "ingest" in msg


def test_a_blank_gate_name_is_rejected():
    with pytest.raises(GateSplitError, match="gate name"):
        split_at_gates(_flow([_s("a"), _s("b", gate="   ")]))


def test_duplicate_gate_names_are_rejected():
    """Approvals are audited by name; two gates sharing one is an ambiguous
    audit trail."""
    with pytest.raises(GateSplitError, match="duplicate"):
        split_at_gates(_flow([_s("a"), _s("b", gate="g"), _s("c", gate="g")]))


# ── the governance invariant ─────────────────────────────────────────────────

def test_has_in_flow_gates_detects_the_marker():
    assert has_in_flow_gates(_flow([_s("a"), _s("b", gate="g")])) is True


def test_compiler_refuses_to_run_a_gated_flow_WHOLE(monkeypatch):
    """THE governance test. Running a gate-carrying flow through the ordinary
    door would execute every stage and never ask anyone — a gate that does not
    gate. It must fail closed at COMPILE time.
    """
    from cairn.core.dataflow.defs import DataflowDefError
    from cairn.core.dataflow.gate_split import assert_runnable_whole

    ungated = _flow([_s("a"), _s("b")], handoff=False)
    assert_runnable_whole(ungated, site="node x")  # no raise

    with pytest.raises(DataflowDefError) as e:
        assert_runnable_whole(_flow([_s("a"), _s("b", gate="g")]), site="node x")
    assert "g" in str(e.value) and "split" in str(e.value).lower()


def test_stageref_still_forbids_unknown_fields():
    """`gate` is additive — the closed schema must stay closed."""
    with pytest.raises(Exception):
        StageRef(stage="a", nonsense=1)


def test_gate_defaults_to_none_so_every_existing_stage_parses():
    assert StageRef(stage="a").gate is None
