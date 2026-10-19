"""The eval scorecard row + the evaluation-cost BUDGET (#2031, ADR-0102 D5).

Two contracts that three operator packs share, so none of them re-invents either:

**The row.** Before this module an eval operator emitted one flat
``{suite/metric: value}`` map — enough for ``model/gate`` to threshold, and
nothing else. A number in that map cannot say WHICH slice of the task it came
from, WHICH case produced it, or WHO graded it, so a regression could be seen
but not located, and a judge's opinion sat in the same namespace as a
deterministic exact-match. :class:`ScorecardRow` carries all four dimensions —
**metric · slice · case · grader** — and the flat map the existing gates consume
is DERIVED from the rows by :func:`flat_metrics`. One authoring, two readers.

**The budget.** The C-eval-benchmarks survey found no platform that estimates an
evaluation's cost up front; the only prior art is promptfoo's per-case ``cost`` /
``latency`` assertions, which are *budgets*. ADR-0102 D5 takes that side
deliberately: :func:`assert_within_budget` RAISES, naming every case that broke a
ceiling. There is no ``enforce=False`` and no warn-only mode — a budget that can
be downgraded to a warning is an estimate with extra steps.

Deliberately NOT here: how a harness is run, how a judge is called, where the
rows are persisted. This module is the vocabulary; the packs supply the verbs.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

#: The metric-name suffix lm-evaluation-harness appends to a score computed with
#: 13-gram overlap decontamination applied (``docs/decontamination.md``; opt-in,
#: a task must override ``should_decontaminate`` +
#: ``doc_to_decontamination_query``). It is part of the NAME on purpose: a
#: decontaminated score is not comparable with the contaminated one, so the two
#: must never collide on one key in the flat map a gate thresholds.
DECONTAM_SUFFIX = "_decontaminate"

#: The two directions ``operator_eval.directions`` (and every gate downstream)
#: understands. Stated here so a row cannot be built with a third.
DIRECTIONS = ("higher_better", "lower_better")


def is_decontaminated(metric: str) -> bool:
    """True when this metric NAME is a decontaminated measurement."""
    return str(metric).endswith(DECONTAM_SUFFIX)


def decontaminated_name(metric: str) -> str:
    """The decontaminated twin of ``metric`` (idempotent)."""
    return metric if is_decontaminated(metric) else f"{metric}{DECONTAM_SUFFIX}"


class ScorecardRow(BaseModel):
    """One measurement, addressable by metric · slice · case · grader.

    A row with an empty ``case`` is an AGGREGATE over the slice — that is the
    only kind :func:`flat_metrics` exposes to a gate, because thresholding a
    single sample is not a quality bar. Per-case rows exist so a regression can
    be located and so the budget has something to bind to.
    """

    metric: str = Field(..., description="Metric name, e.g. 'exact_match' or 'judge_score'.")
    value: float = Field(..., description="The measurement.")
    direction: str = Field(
        "higher_better",
        description="higher_better | lower_better — which way 'worse' points, for the gates.",
    )
    slice: str = Field(
        "",
        description="The task / split / subset this was measured on ('gsm8k', 'arithmetic', "
                    "'control'). Empty = the whole evaluation.",
    )
    case: str = Field(
        "",
        description="The individual sample id this row describes. EMPTY = an aggregate over the "
                    "slice, and only aggregates reach flat_metrics(). For an Inspect run this is "
                    "the stable sample id that makes eval_set() resume work; for a registry-backed "
                    "eval it is the registry ROW id.",
    )
    grader: str = Field(
        "",
        description="Who produced the number: a harness ('lm-eval', 'inspect'), a judge model "
                    "('gpt-4o-mini'), or 'deterministic'. The dimension that lets a scorecard "
                    "separate a measured fact from an opinion.",
    )
    unit: str = Field("", description="Display unit, when the metric has one.")
    decontaminated: bool = Field(
        False,
        description="This score had 13-gram decontamination applied. Must be reflected in the "
                    "metric NAME (DECONTAM_SUFFIX) — see the validator.",
    )
    untrusted_metric: bool = Field(
        False,
        description="Judge-derived (ADR-0095 §2.6 / ADR-0102 D3): may CONTRIBUTE to a gate, "
                    "never satisfy one alone. Set from the judge-trust badge, not guessed.",
    )
    cost_usd: float = Field(0.0, ge=0.0, description="What producing this row cost, when known.")
    latency_ms: float = Field(0.0, ge=0.0, description="Wall-clock for this row, when known.")
    extras: dict[str, Any] = Field(
        default_factory=dict, description="Grader-specific detail (agreement record, stderr, …)."
    )

    @field_validator("direction")
    @classmethod
    def _known_direction(cls, v: str) -> str:
        if v not in DIRECTIONS:
            raise ValueError(f"direction must be one of {DIRECTIONS}, got {v!r}")
        return v

    @model_validator(mode="after")
    def _decontamination_is_in_the_name(self) -> "ScorecardRow":
        # A decontaminated score under the clean metric's key would silently
        # replace an incomparable number — the exact failure the suffix exists
        # to prevent. Fail at construction, not at the gate.
        if self.decontaminated and not is_decontaminated(self.metric):
            raise ValueError(
                f"row is marked decontaminated but metric {self.metric!r} does not end in "
                f"{DECONTAM_SUFFIX!r}; a decontaminated score must not share a key with the "
                f"contaminated one"
            )
        return self

    @property
    def key(self) -> str:
        """The flat-map key: ``slice/metric``, or ``metric`` when there is no slice."""
        return f"{self.slice}/{self.metric}" if self.slice else self.metric


def flat_metrics(rows: Iterable[ScorecardRow]) -> dict[str, float]:
    """The ``{slice/metric: value}`` map ``model/gate`` / ``eval/delta`` consume.

    AGGREGATE rows only (``case == ""``). Later rows win on a repeated key, which
    only happens when a caller emits the same aggregate twice.
    """
    return {r.key: float(r.value) for r in rows if not r.case}


def directions_of(rows: Iterable[ScorecardRow]) -> dict[str, str]:
    """The ``{slice/metric: direction}`` map that keeps a gate from guessing."""
    return {r.key: r.direction for r in rows if not r.case}


class EvalBudget(BaseModel):
    """Ceilings on what an evaluation may spend. ``0`` = that bound is not set.

    Per-CASE bounds are the useful ones: a run that is 3× over on one prompt is a
    broken prompt, and a total-only ceiling would hide it behind the average.
    There is deliberately no ``enforce`` flag — see the module docstring.
    """

    max_cost_usd_per_case: float = Field(
        0.0, ge=0.0, description="Ceiling on one case's cost. 0 = unbounded.")
    max_latency_ms_per_case: float = Field(
        0.0, ge=0.0, description="Ceiling on one case's wall-clock. 0 = unbounded.")
    max_total_cost_usd: float = Field(
        0.0, ge=0.0, description="Ceiling on the whole evaluation's cost. 0 = unbounded.")

    @property
    def bounded(self) -> bool:
        return bool(self.max_cost_usd_per_case or self.max_latency_ms_per_case
                    or self.max_total_cost_usd)


class EvalBudgetExceeded(RuntimeError):
    """An evaluation broke a declared ceiling. Carries every violation."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__(
            "evaluation budget exceeded: " + "; ".join(violations)
        )


def assert_within_budget(
    rows: Iterable[ScorecardRow], budget: Optional[EvalBudget]
) -> None:
    """Raise :class:`EvalBudgetExceeded` when any declared ceiling is broken.

    Every violation is collected before raising, so one run reports all of them
    rather than the alphabetically-first. Returns ``None`` when nothing is bound.
    """
    if budget is None or not budget.bounded:
        return None
    rows = list(rows)
    violations: list[str] = []
    total = 0.0
    for r in rows:
        total += float(r.cost_usd)
        label = r.case or r.key
        if budget.max_cost_usd_per_case and r.cost_usd > budget.max_cost_usd_per_case:
            violations.append(
                f"case {label!r} cost ${r.cost_usd:.4f} > ${budget.max_cost_usd_per_case:.4f}")
        if budget.max_latency_ms_per_case and r.latency_ms > budget.max_latency_ms_per_case:
            violations.append(
                f"case {label!r} latency {r.latency_ms:.0f}ms > "
                f"{budget.max_latency_ms_per_case:.0f}ms")
    if budget.max_total_cost_usd and total > budget.max_total_cost_usd:
        violations.append(
            f"total cost ${total:.4f} > ${budget.max_total_cost_usd:.4f}")
    if violations:
        raise EvalBudgetExceeded(violations)
    return None


__all__ = [
    "DECONTAM_SUFFIX",
    "DIRECTIONS",
    "EvalBudget",
    "EvalBudgetExceeded",
    "ScorecardRow",
    "assert_within_budget",
    "decontaminated_name",
    "directions_of",
    "flat_metrics",
    "is_decontaminated",
]
