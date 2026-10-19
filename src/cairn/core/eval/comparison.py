"""§24.11.4 regression eval: baseline I/O + current-vs-baseline comparison.

Workflow:

  1. ``cairn eval <pack> --save-baseline <file>``
     Runs the eval; persists each case's actual output + cost + template
     hash to ``<file>``. This is the "known good" snapshot.

  2. (Author edits prompts / changes model / etc.)

  3. ``cairn eval <pack> --compare-against <file>``
     Runs the eval again; for each case present in both current and
     baseline, compares actual outputs using the subset scorer. Reports
     agree / differ per case + cost delta + duration delta. Optional
     ``--threshold <pct>`` exits non-zero if agreement falls below.

This is the killer "did I break prod?" feature. Same pattern as
snapshot tests in normal CI — record a known-good state, compare
edits against it, gate the PR.

What this is NOT:

  - Re-running the historical template (that needs §24.11.3c template
    registry — not built yet).
  - Statistical significance testing (deferred — Phase 2 of §24.11.4).
  - Judge-LLM semantic comparison (deferred — Phase 2 of §24.11.4).

The baseline is a file; copy it to source control, share via PR
review, version it however you version other test fixtures.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from cairn.core.eval.scorer import EvalResult, EvalSummary, score_case

logger = logging.getLogger(__name__)


# ─── Baseline shape ─────────────────────────────────────────────────


class BaselineEntry(BaseModel):
    """One case's snapshot in a baseline file."""
    name: str
    status: str
    actual: dict[str, Any]
    matched_skills: int = 0
    cost_usd: Optional[float] = None
    duration_seconds: float = 0.0
    template_hash: str = ""
    run_id: str = ""


class Baseline(BaseModel):
    """A point-in-time snapshot of eval results for one pack."""
    pack: str
    saved_at: str
    cases: dict[str, BaselineEntry] = Field(default_factory=dict)

    @classmethod
    def from_summary(cls, pack: str, summary: EvalSummary) -> "Baseline":
        """Build a Baseline from an in-hand :class:`EvalSummary`."""
        return cls(
            pack=pack,
            saved_at=datetime.now(timezone.utc).isoformat(),
            cases={
                r.name: BaselineEntry(
                    name=r.name,
                    status=r.status,
                    actual=r.actual,
                    matched_skills=r.matched_skills,
                    cost_usd=r.cost_usd,
                    duration_seconds=r.duration_seconds,
                    template_hash=r.template_hash,
                    run_id=r.run_id,
                )
                for r in summary.results
            },
        )


def save_baseline(baseline: Baseline, path: Path) -> None:
    """Write a baseline JSON file. Creates parent dirs if missing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(baseline.model_dump_json(indent=2))
    logger.info(
        "baseline saved path=%s pack=%s cases=%d",
        path, baseline.pack, len(baseline.cases),
    )


def load_baseline(path: Path) -> Baseline:
    """Read a baseline JSON file. Raises FileNotFoundError if absent,
    pydantic ValidationError if shape is wrong."""
    if not path.exists():
        raise FileNotFoundError(f"baseline file not found: {path}")
    return Baseline.model_validate_json(path.read_text())


# ─── Comparison ─────────────────────────────────────────────────────


class CaseComparison(BaseModel):
    """One case's current vs baseline comparison."""
    name: str
    #: ``"agree"`` — current actual is a subset-match to baseline actual.
    #: ``"differ"`` — current diverges (mismatches non-empty).
    #: ``"only_current"`` — case exists in current run but not in baseline.
    #: ``"only_baseline"`` — case in baseline but not in current run.
    #: ``"error"`` — current case errored; comparison N/A.
    verdict: str
    mismatches: list[str] = Field(default_factory=list)
    cost_delta_usd: Optional[float] = None
    duration_delta_seconds: float = 0.0
    current_run_id: str = ""
    baseline_run_id: str = ""
    template_hash_changed: bool = False


class ComparisonReport(BaseModel):
    """Aggregate of a current-vs-baseline comparison."""
    pack: str
    baseline_path: str
    total: int
    agreed: int
    differed: int
    only_current: int
    only_baseline: int
    errored: int
    agreement_pct: float
    cost_delta_total_usd: Optional[float] = None
    comparisons: list[CaseComparison] = Field(default_factory=list)

    def passes_threshold(self, threshold_pct: Optional[float]) -> bool:
        """``True`` if the run meets ``threshold_pct`` agreement.

        ``None`` threshold → any non-error run passes. Threshold is in
        percent (e.g. ``80.0`` means "at least 80% of comparable cases
        must agree").
        """
        if threshold_pct is None:
            return self.errored == 0
        return self.agreement_pct >= threshold_pct and self.errored == 0


def compare_to_baseline(
    summary: EvalSummary,
    baseline: Baseline,
) -> ComparisonReport:
    """Build a :class:`ComparisonReport` from a current run + a baseline.

    Comparison semantics per case:

    - Both present → score current.actual against baseline.actual via the
      subset scorer. Empty mismatches = ``"agree"``; non-empty = ``"differ"``.
    - Case present in current only → ``"only_current"`` (a new case the
      author added since the baseline was saved).
    - Case present in baseline only → ``"only_baseline"`` (a case the
      author removed; baseline is now stale for it).
    - Current errored → ``"error"``; deltas not computed.
    """
    current_by_name = {r.name: r for r in summary.results}
    baseline_names = set(baseline.cases.keys())
    current_names = set(current_by_name.keys())

    comparisons: list[CaseComparison] = []
    cost_delta_total = 0.0
    have_costs = False

    for name in sorted(current_names | baseline_names):
        in_current = name in current_by_name
        in_baseline = name in baseline_names

        if not in_baseline:
            comparisons.append(CaseComparison(
                name=name,
                verdict="only_current",
                current_run_id=current_by_name[name].run_id,
            ))
            continue

        if not in_current:
            comparisons.append(CaseComparison(
                name=name,
                verdict="only_baseline",
                baseline_run_id=baseline.cases[name].run_id,
            ))
            continue

        current = current_by_name[name]
        base = baseline.cases[name]

        if current.status == "error":
            comparisons.append(CaseComparison(
                name=name,
                verdict="error",
                current_run_id=current.run_id,
                baseline_run_id=base.run_id,
                template_hash_changed=(current.template_hash != base.template_hash),
            ))
            continue

        # Use the subset scorer: treat the baseline's actual as the
        # "expected" shape. Empty mismatches = current is a faithful
        # superset of what the baseline saw.
        mismatches = score_case(base.actual, current.actual)

        cost_delta = None
        if current.cost_usd is not None and base.cost_usd is not None:
            cost_delta = current.cost_usd - base.cost_usd
            cost_delta_total += cost_delta
            have_costs = True

        comparisons.append(CaseComparison(
            name=name,
            verdict="agree" if not mismatches else "differ",
            mismatches=mismatches,
            cost_delta_usd=cost_delta,
            duration_delta_seconds=current.duration_seconds - base.duration_seconds,
            current_run_id=current.run_id,
            baseline_run_id=base.run_id,
            template_hash_changed=(current.template_hash != base.template_hash),
        ))

    agreed = sum(1 for c in comparisons if c.verdict == "agree")
    differed = sum(1 for c in comparisons if c.verdict == "differ")
    only_current = sum(1 for c in comparisons if c.verdict == "only_current")
    only_baseline = sum(1 for c in comparisons if c.verdict == "only_baseline")
    errored = sum(1 for c in comparisons if c.verdict == "error")

    # Agreement % over the cases comparable in both sides (both present
    # AND current did not error). only_current / only_baseline aren't
    # counted in the denominator — they're structural changes, not
    # quality regressions.
    comparable = agreed + differed
    agreement = (100.0 * agreed / comparable) if comparable > 0 else 100.0

    return ComparisonReport(
        pack=baseline.pack,
        baseline_path="",  # filled in by the CLI command from the actual path
        total=len(comparisons),
        agreed=agreed,
        differed=differed,
        only_current=only_current,
        only_baseline=only_baseline,
        errored=errored,
        agreement_pct=round(agreement, 1),
        cost_delta_total_usd=round(cost_delta_total, 4) if have_costs else None,
        comparisons=comparisons,
    )
