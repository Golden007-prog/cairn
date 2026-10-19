"""Tests for §24.11.4 regression eval: baseline I/O + comparison.

Pure-logic tests — no Runner / LLM. Mocked summaries and baselines.
The end-to-end "save then compare" path is structural; the LLM-driven
part is the same machinery tested in test_eval.py.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.eval import (
    Baseline,
    BaselineEntry,
    EvalResult,
    EvalSummary,
    compare_to_baseline,
    load_baseline,
    save_baseline,
)


# ─── Baseline round-trip ──────────────────────────────────────────────


def test_baseline_save_and_load_round_trip(tmp_path):
    """Pydantic model_dump_json → file → model_validate_json yields equal
    objects."""
    summary = EvalSummary(
        total=2, passed=2, failed=0, errored=0,
        results=[
            EvalResult(
                name="c1", status="pass", run_id="run-1",
                actual={"matched_skills": 1},
                cost_usd=0.04, duration_seconds=1.2,
                template_hash="sha256:abc",
            ),
            EvalResult(
                name="c2", status="pass", run_id="run-2",
                actual={"matched_skills": 0},
                cost_usd=0.02, duration_seconds=0.5,
            ),
        ],
    )
    b = Baseline.from_summary("incident", summary)
    path = tmp_path / "baseline.json"
    save_baseline(b, path)
    loaded = load_baseline(path)

    assert loaded.pack == "incident"
    assert set(loaded.cases.keys()) == {"c1", "c2"}
    assert loaded.cases["c1"].cost_usd == 0.04
    assert loaded.cases["c1"].template_hash == "sha256:abc"
    assert loaded.cases["c1"].actual == {"matched_skills": 1}


def test_load_baseline_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_baseline(tmp_path / "no-such-file.json")


def test_save_baseline_creates_parent_dirs(tmp_path):
    """Save into a nested path that doesn't exist yet."""
    nested = tmp_path / "evals" / "baselines" / "pre-pr.json"
    summary = EvalSummary(total=0, passed=0, failed=0, errored=0)
    b = Baseline.from_summary("incident", summary)
    save_baseline(b, nested)
    assert nested.exists()
    assert json.loads(nested.read_text())["pack"] == "incident"


# ─── Comparison verdicts ──────────────────────────────────────────────


def _make_summary(*results: EvalResult) -> EvalSummary:
    return EvalSummary(
        total=len(results),
        passed=sum(1 for r in results if r.status == "pass"),
        failed=sum(1 for r in results if r.status == "fail"),
        errored=sum(1 for r in results if r.status == "error"),
        results=list(results),
    )


def _make_baseline(pack: str, *entries: tuple[str, dict, float | None]) -> Baseline:
    """Build a baseline with (name, actual, cost) tuples."""
    cases = {
        name: BaselineEntry(name=name, status="pass", actual=actual, cost_usd=cost)
        for name, actual, cost in entries
    }
    return Baseline(pack=pack, saved_at="2026-01-01T00:00:00Z", cases=cases)


def test_compare_all_agree():
    """Current matches baseline exactly → 100% agreement."""
    summary = _make_summary(
        EvalResult(name="c1", status="pass", run_id="r1",
                   actual={"matched_skills": 1}, cost_usd=0.04),
    )
    baseline = _make_baseline("incident", ("c1", {"matched_skills": 1}, 0.06))
    report = compare_to_baseline(summary, baseline)

    assert report.agreed == 1
    assert report.differed == 0
    assert report.agreement_pct == 100.0
    assert report.comparisons[0].verdict == "agree"
    # cost delta: 0.04 - 0.06 = -0.02 (negative = saved)
    assert report.cost_delta_total_usd == -0.02


def test_compare_differ_reports_mismatches():
    """Current diverges from baseline → differ + mismatches list."""
    summary = _make_summary(
        EvalResult(name="c1", status="pass", run_id="r1",
                   actual={"matched_skills": 0}),
    )
    baseline = _make_baseline("incident", ("c1", {"matched_skills": 1}, None))
    report = compare_to_baseline(summary, baseline)

    assert report.differed == 1
    assert report.agreement_pct == 0.0
    cmp = report.comparisons[0]
    assert cmp.verdict == "differ"
    assert any("matched_skills" in m for m in cmp.mismatches)


def test_compare_only_current_for_new_case():
    """Case in current but not baseline → only_current, not counted in
    agreement."""
    summary = _make_summary(
        EvalResult(name="c1", status="pass", run_id="r1", actual={"a": 1}),
        EvalResult(name="c2_new", status="pass", run_id="r2", actual={"b": 2}),
    )
    baseline = _make_baseline("incident", ("c1", {"a": 1}, None))
    report = compare_to_baseline(summary, baseline)

    assert report.only_current == 1
    assert report.agreed == 1
    # only_current is structural, not a regression — agreement stays 100%
    assert report.agreement_pct == 100.0


def test_compare_only_baseline_for_removed_case():
    """Case in baseline but not current → only_baseline."""
    summary = _make_summary(
        EvalResult(name="c1", status="pass", run_id="r1", actual={"a": 1}),
    )
    baseline = _make_baseline(
        "incident",
        ("c1", {"a": 1}, None),
        ("c2_removed", {"b": 2}, None),
    )
    report = compare_to_baseline(summary, baseline)

    assert report.only_baseline == 1
    assert report.agreed == 1
    # only_baseline is structural; doesn't drop agreement
    assert report.agreement_pct == 100.0


def test_compare_error_case_not_counted_as_differ():
    """Pipeline-errored cases → verdict='error', distinct from differ."""
    summary = _make_summary(
        EvalResult(name="c1", status="error", run_id="r1",
                   error="LLM 401", actual={}),
    )
    baseline = _make_baseline("incident", ("c1", {"matched_skills": 1}, 0.05))
    report = compare_to_baseline(summary, baseline)

    assert report.errored == 1
    assert report.differed == 0
    assert report.comparisons[0].verdict == "error"


def test_compare_template_hash_change_flagged():
    """When the same case ran with a different template hash, flag it
    so the operator knows the prompt version changed."""
    summary = _make_summary(
        EvalResult(name="c1", status="pass", run_id="r1",
                   actual={"matched_skills": 1},
                   template_hash="sha256:NEW"),
    )
    baseline = Baseline(
        pack="incident",
        saved_at="2026-01-01T00:00:00Z",
        cases={
            "c1": BaselineEntry(
                name="c1", status="pass", actual={"matched_skills": 1},
                template_hash="sha256:OLD",
            ),
        },
    )
    report = compare_to_baseline(summary, baseline)
    assert report.comparisons[0].template_hash_changed is True


def test_compare_template_hash_unchanged():
    """Same hash → flag False."""
    summary = _make_summary(
        EvalResult(name="c1", status="pass", run_id="r1",
                   actual={"a": 1}, template_hash="sha256:X"),
    )
    baseline = Baseline(
        pack="incident",
        saved_at="2026-01-01T00:00:00Z",
        cases={
            "c1": BaselineEntry(name="c1", status="pass",
                                actual={"a": 1}, template_hash="sha256:X"),
        },
    )
    report = compare_to_baseline(summary, baseline)
    assert report.comparisons[0].template_hash_changed is False


# ─── Threshold gating (CI integration) ────────────────────────────────


def test_threshold_none_passes_when_no_errors():
    """Without --threshold, any non-errored run passes the gate."""
    summary = _make_summary(
        EvalResult(name="c1", status="pass", run_id="r1", actual={"a": 1}),
        EvalResult(name="c2", status="pass", run_id="r2", actual={"a": 999}),
    )
    baseline = _make_baseline(
        "incident",
        ("c1", {"a": 1}, None),
        ("c2", {"a": 2}, None),  # will differ
    )
    report = compare_to_baseline(summary, baseline)
    assert report.passes_threshold(None) is True
    # 1/2 agree = 50%
    assert report.agreement_pct == 50.0


def test_threshold_80_fails_at_50_pct_agreement():
    summary = _make_summary(
        EvalResult(name="c1", status="pass", run_id="r1", actual={"a": 1}),
        EvalResult(name="c2", status="pass", run_id="r2", actual={"a": 999}),
    )
    baseline = _make_baseline(
        "incident",
        ("c1", {"a": 1}, None),
        ("c2", {"a": 2}, None),
    )
    report = compare_to_baseline(summary, baseline)
    assert report.passes_threshold(80.0) is False


def test_threshold_50_passes_at_50_pct_agreement():
    """Boundary — equal-to threshold passes."""
    summary = _make_summary(
        EvalResult(name="c1", status="pass", run_id="r1", actual={"a": 1}),
        EvalResult(name="c2", status="pass", run_id="r2", actual={"a": 999}),
    )
    baseline = _make_baseline(
        "incident",
        ("c1", {"a": 1}, None),
        ("c2", {"a": 2}, None),
    )
    report = compare_to_baseline(summary, baseline)
    assert report.passes_threshold(50.0) is True


def test_threshold_errored_run_always_fails():
    """An errored case fails the threshold gate regardless of agreement
    on the remaining cases. Errors are blocking signals."""
    summary = _make_summary(
        EvalResult(name="c1", status="pass", run_id="r1", actual={"a": 1}),
        EvalResult(name="c2", status="error", run_id="r2", error="LLM 500"),
    )
    baseline = _make_baseline(
        "incident",
        ("c1", {"a": 1}, None),
        ("c2", {"a": 1}, None),
    )
    report = compare_to_baseline(summary, baseline)
    # c1 agrees, c2 errored. Comparable = 1. Agreement = 100%.
    # But errored > 0 → threshold gate fails.
    assert report.agreement_pct == 100.0
    assert report.passes_threshold(80.0) is False
    assert report.passes_threshold(None) is False
