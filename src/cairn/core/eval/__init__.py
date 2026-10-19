"""Offline evaluation: dataset-driven scoring for pack templates.

§24.5 of the Roadmap (the missing CLI command that closes the original
MVP six). Reuses :mod:`cairn.cli.commands.run`'s pipeline per case so
each eval case produces a real ``run_id`` and JSONL trace — failed cases
are debuggable via ``cairn replay <run-id>``.

MVP scope (locked in §24.5):
  - Schema validity (did the pipeline produce a structured output)
  - Subset-style structural matching (expected ⊂ actual, dot-paths)
  - Per-case pass/fail/error + cost + run_id

Deferred to §24.11.4 / future:
  - Judge-LLM semantic scoring
  - ``--compare-against <tag>`` regression eval
  - Statistical significance
  - Auto-rollback / CI gates

Public surface:
  - :class:`EvalCase` / :class:`EvalResult` — typed shapes
  - :func:`load_dataset` — read one ``*.jsonl`` file
  - :func:`load_pack_datasets` — discover all datasets in a pack's evals_dir
  - :func:`score_case` — compare expected ⊂ actual, return mismatches
"""

from __future__ import annotations

from cairn.core.eval.comparison import (
    Baseline,
    BaselineEntry,
    CaseComparison,
    ComparisonReport,
    compare_to_baseline,
    load_baseline,
    save_baseline,
)
from cairn.core.eval.scorer import (
    EvalCase,
    EvalResult,
    EvalSummary,
    load_dataset,
    load_pack_datasets,
    score_case,
)
# ADR-0057 (#442) — the scoring-pass dispatcher. Imported lazily inside a
# function-free re-export would be ideal, but recipe.py only pulls core.operator
# (already loaded by any eval path), so a top-level import is fine.
from cairn.core.eval.recipe import (
    EvalRecipeError,
    EvalRecipeResult,
    extract_run_output,
    resolve_runtime_contract,
    resolve_runtime_recipe,
    resolve_test_recipe,
    run_eval_recipe,
)
# #474 — the failure taxonomy (pure functions; no storage import, see module
# docstring). Part of the failure-driven eval epic (#480, ADR-0059).
from cairn.core.eval.taxonomy import (
    FailureLabel,
    FailureType,
    classify_failure,
    classify_failure_llm,
)

__all__ = [
    "Baseline",
    "BaselineEntry",
    "CaseComparison",
    "ComparisonReport",
    "EvalCase",
    "EvalRecipeError",
    "EvalRecipeResult",
    "EvalResult",
    "EvalSummary",
    "FailureLabel",
    "FailureType",
    "classify_failure",
    "classify_failure_llm",
    "compare_to_baseline",
    "extract_run_output",
    "load_baseline",
    "load_dataset",
    "load_pack_datasets",
    "resolve_runtime_contract",
    "resolve_runtime_recipe",
    "resolve_test_recipe",
    "run_eval_recipe",
    "score_case",
]
