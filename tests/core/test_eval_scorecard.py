"""The eval scorecard contract + the evaluation-cost BUDGET (#2031, ADR-0102 D5).

Three properties, each with a test that goes red when the behaviour is removed:

1. **A scorecard row carries four dimensions** — metric · slice · case · grader —
   so "0.71" can be read as "gsm8k exact_match, arithmetic slice, case 41, graded
   by lm-eval". The flat ``{slice/metric: value}`` map the eval pack's gates
   already consume is DERIVED from the rows, never authored twice.
2. **Decontamination is visible in the metric name.** lm-eval's 13-gram
   decontamination produces a ``_decontaminate`` metric suffix; a row that came
   from a decontaminated measurement says so, and the suffix survives into the
   flat map so a gate threshold can be written against it.
3. **The evaluation budget FAILS LOUDLY.** Per-case cost and latency ceilings
   raise :class:`EvalBudgetExceeded` naming every offending case — a budget, not
   an estimate (the research doc's gap: no surveyed platform estimates eval cost
   up front; promptfoo's per-case assertions are the only prior art).
"""
from __future__ import annotations

import pytest

from cairn.core.eval.scorecard import (
    DECONTAM_SUFFIX,
    EvalBudget,
    EvalBudgetExceeded,
    ScorecardRow,
    assert_within_budget,
    directions_of,
    flat_metrics,
    is_decontaminated,
)


def _rows() -> list[ScorecardRow]:
    return [
        ScorecardRow(metric="exact_match", value=0.71, slice="gsm8k", grader="lm-eval"),
        ScorecardRow(
            metric="exact_match" + DECONTAM_SUFFIX, value=0.64, slice="gsm8k", grader="lm-eval",
            decontaminated=True,
        ),
        ScorecardRow(
            metric="exact_match", value=1.0, slice="gsm8k", case="row-41", grader="lm-eval",
            cost_usd=0.004, latency_ms=900.0,
        ),
        ScorecardRow(
            metric="exact_match", value=0.0, slice="gsm8k", case="row-42", grader="lm-eval",
            cost_usd=0.02, latency_ms=4200.0,
        ),
    ]


# ── 1. the four dimensions, and the flat map derived from them ──────────


def test_a_row_carries_metric_slice_case_and_grader():
    row = _rows()[2]
    assert (row.metric, row.slice, row.case, row.grader) == (
        "exact_match", "gsm8k", "row-41", "lm-eval")


def test_flat_metrics_uses_only_the_aggregate_rows():
    """Per-case rows are detail, not gate input: a gate that read them would be
    thresholding one sample. Only rows with no ``case`` reach the flat map."""
    flat = flat_metrics(_rows())
    assert flat == {"gsm8k/exact_match": 0.71, f"gsm8k/exact_match{DECONTAM_SUFFIX}": 0.64}


def test_flat_metrics_drops_the_slice_prefix_when_there_is_no_slice():
    flat = flat_metrics([ScorecardRow(metric="judge_score", value=0.8, grader="gpt-4o-mini")])
    assert flat == {"judge_score": 0.8}


def test_directions_are_reported_per_flat_key():
    rows = [
        ScorecardRow(metric="exact_match", value=0.7, slice="gsm8k"),
        ScorecardRow(metric="perplexity", value=12.0, slice="wikitext", direction="lower_better"),
    ]
    assert directions_of(rows) == {
        "gsm8k/exact_match": "higher_better", "wikitext/perplexity": "lower_better"}


def test_a_row_refuses_an_unknown_direction():
    with pytest.raises(ValueError):
        ScorecardRow(metric="acc", value=0.5, direction="bigger_is_nicer")


# ── 2. decontamination is visible ───────────────────────────────────────


def test_the_decontamination_suffix_is_recognised_by_name():
    assert is_decontaminated("acc" + DECONTAM_SUFFIX)
    assert not is_decontaminated("acc")


def test_a_decontaminated_row_must_carry_the_suffix():
    """A row flagged decontaminated whose metric name hides it would put an
    UNCOMPARABLE number under a clean metric's key."""
    with pytest.raises(ValueError):
        ScorecardRow(metric="acc", value=0.5, decontaminated=True)


def test_the_suffix_survives_into_the_flat_map():
    flat = flat_metrics(_rows())
    assert any(is_decontaminated(k) for k in flat)


# ── 3. the budget fails loudly ──────────────────────────────────────────


def test_per_case_cost_ceiling_raises_naming_the_case():
    with pytest.raises(EvalBudgetExceeded) as exc:
        assert_within_budget(_rows(), EvalBudget(max_cost_usd_per_case=0.01))
    assert "row-42" in str(exc.value)
    assert "row-41" not in str(exc.value)
    assert exc.value.violations


def test_per_case_latency_ceiling_raises_naming_the_case():
    with pytest.raises(EvalBudgetExceeded) as exc:
        assert_within_budget(_rows(), EvalBudget(max_latency_ms_per_case=1000.0))
    assert "row-42" in str(exc.value)


def test_total_cost_ceiling_raises():
    with pytest.raises(EvalBudgetExceeded) as exc:
        assert_within_budget(_rows(), EvalBudget(max_total_cost_usd=0.01))
    assert "total" in str(exc.value)


def test_an_unset_budget_bounds_nothing():
    assert_within_budget(_rows(), EvalBudget()) is None


def test_a_budget_is_never_silently_downgraded_to_a_warning():
    """The point of D5: a budget, not an estimate. There is no 'enforce=false'
    escape on the assertion — the caller either passes a bound or does not."""
    assert "enforce" not in EvalBudget.model_fields
