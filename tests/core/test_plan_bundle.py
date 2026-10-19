"""ADR-0069 (#746) Phase 1 — plan_bundle: turn a BundlePipeline into a validated,
topologically-ordered PipelinePlan. Pure function, no execution.
"""
from __future__ import annotations

import pytest

from cairn.core.engine.bundle import BundlePipeline, BundlePipelineStep
from cairn.core.engine.bundle_pipeline import PipelinePlanError, plan_bundle


def _step(id, *, depends_on=None, gate=None, inputs=None, trigger="auto", **kw):
    return BundlePipelineStep(
        id=id, pack="p", template=id, trigger=trigger,
        depends_on=depends_on or [], gate=gate, inputs=inputs or {}, **kw,
    )


def _pipe(*steps, name="pl"):
    return BundlePipeline(name=name, step=list(steps))


# ─── implicit-linear (back-compat: no depends_on) ──────────────────────────


def test_implicit_linear_order_and_deps():
    plan = plan_bundle(_pipe(_step("a"), _step("b"), _step("c")))
    assert [s.id for s in plan.steps] == ["a", "b", "c"]
    assert plan.step("a").depends_on == []          # first = entrypoint
    assert plan.step("b").depends_on == ["a"]       # implicit: prev step
    assert plan.step("c").depends_on == ["b"]
    assert plan.entrypoints == ["a"]


def test_defaults_are_backcompat():
    # a bare step (no trigger/depends_on) → trigger 'auto', no deps carried
    plan = plan_bundle(_pipe(_step("only")))
    s = plan.step("only")
    assert s.trigger == "auto" and s.depends_on == [] and s.approval is False


# ─── explicit DAG + joins ──────────────────────────────────────────────────


def test_explicit_dag_topo_order():
    # d depends on b and c; both depend on a → a before {b,c} before d
    plan = plan_bundle(_pipe(
        _step("a"),
        _step("b", depends_on=["a"]),
        _step("c", depends_on=["a"]),
        _step("d", depends_on=["b", "c"]),
    ))
    order = [s.id for s in plan.steps]
    assert order.index("a") < order.index("b") < order.index("d")
    assert order.index("a") < order.index("c") < order.index("d")
    assert set(plan.step("d").depends_on) == {"b", "c"}   # a join
    assert plan.entrypoints == ["a"]


# ─── validation (fail-closed) ──────────────────────────────────────────────


def test_unknown_dependency_rejected():
    with pytest.raises(PipelinePlanError, match="unknown step"):
        plan_bundle(_pipe(_step("a"), _step("b", depends_on=["ghost"])))


def test_cycle_rejected():
    with pytest.raises(PipelinePlanError, match="cycle"):
        plan_bundle(_pipe(
            _step("a", depends_on=["b"]),
            _step("b", depends_on=["a"]),
        ))


def test_duplicate_step_id_rejected():
    with pytest.raises(PipelinePlanError, match="duplicate"):
        plan_bundle(_pipe(_step("a"), _step("a")))


def test_gate_wire_must_reference_a_dependency():
    # b gates on a step it doesn't depend on → rejected (a is not guaranteed to run)
    with pytest.raises(PipelinePlanError, match="does not depend"):
        plan_bundle(_pipe(
            _step("a"),
            _step("b"),  # implicit dep = a
            _step("c", depends_on=["a"], gate="$steps.b.verdict == 'pass'"),  # b not upstream of c
        ))


def test_input_wire_to_real_upstream_ok():
    plan = plan_bundle(_pipe(
        _step("prep"),
        _step("train", depends_on=["prep"], inputs={"dataset": "$steps.prep.staged_dataset_uri"}),
    ))
    assert plan.step("train").inputs["dataset"] == "$steps.prep.staged_dataset_uri"


def test_input_wire_to_unknown_step_rejected():
    with pytest.raises(PipelinePlanError, match="unknown step"):
        plan_bundle(_pipe(
            _step("prep"),
            _step("train", depends_on=["prep"], inputs={"dataset": "$steps.ghost.uri"}),
        ))


# ─── trigger validation (on the schema) ────────────────────────────────────


def test_valid_trigger_forms():
    for t in ("auto", "manual", "schedule:0 * * * *", "on:model.trained"):
        assert _step("x", trigger=t).trigger == t


def test_invalid_trigger_rejected():
    with pytest.raises(ValueError, match="invalid trigger"):
        _step("x", trigger="whenever")
