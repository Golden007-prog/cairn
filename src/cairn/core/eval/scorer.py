"""Dataset loading + subset-style structural scoring.

Pure logic — no Runner / LLM / pipeline. Tested in isolation.

**Subset semantics** (matters): the scorer asks "is ``expected`` a
subset of ``actual``?", not "are they equal." Every leaf path in
expected must exist in actual with the same value; extra paths in
actual are fine. This matches how tests in the wild are written:
the author asserts what they care about, ignores what they don't.

```
expected = {"root_cause": {"category": "deploy"}, "matched_skills": 1}
actual   = {"root_cause": {"category": "deploy", "confidence": 0.9},
            "matched_skills": 1, "evidence_count": 4}
→ pass (every expected path exists in actual with matching value)

expected = {"root_cause": {"category": "deploy"}}
actual   = {"root_cause": {"category": "network"}}
→ fail (mismatch at root_cause.category: deploy != network)
```
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from cairn.core.engine.schema import TriggerEvent


# ─── Models ─────────────────────────────────────────────────────────


class EvalCase(BaseModel):
    """One row from an ``evals/*.jsonl`` dataset.

    ``trigger`` is parsed strictly into :class:`TriggerEvent` so authoring
    mistakes fail at load time, not mid-run.

    ``expected`` is an arbitrary nested dict. The scorer walks every leaf
    path in ``expected`` and verifies it appears in the actual pipeline
    output (results summary + last ``findings`` event per skill) with the
    same value.
    """
    name: str
    trigger: TriggerEvent
    expected: dict[str, Any] = Field(default_factory=dict)
    description: Optional[str] = None
    tags: list[str] = Field(default_factory=list)


class EvalResult(BaseModel):
    """Outcome of running + scoring one case.

    ``status`` distinguishes:
      - ``"pass"``    — case ran AND every expected path matched
      - ``"fail"``    — case ran BUT some expected path didn't match
      - ``"error"``   — pipeline raised; couldn't score
      - ``"skip"``    — operator opt-out (future)

    ``mismatches`` is empty on pass; populated with dot-path explanations
    on fail. ``run_id`` is always set (even on error) so
    ``cairn replay <run-id>`` debugs any case.

    ``actual`` is the dict the scorer compared against (matched_skills,
    skills summary, etc.) — the §24.11.4 baseline workflow stores this
    so future eval runs can diff against it.
    """
    name: str
    status: str
    run_id: str
    mismatches: list[str] = Field(default_factory=list)
    error: Optional[str] = None
    duration_seconds: float = 0.0
    matched_skills: int = 0
    #: §24.11.4: the dict the scorer compared expected against — used as
    #: the baseline when this run is saved via ``--save-baseline``.
    actual: dict[str, Any] = Field(default_factory=dict)
    #: Optional cost. Populated when the pipeline reports it; left None
    #: when offline / unknown. Used for cost-delta in comparison.
    cost_usd: Optional[float] = None
    #: §24.11.3b — the template content hash that ran for the first
    #: matched skill (or empty if no skills fired / older runner).
    #: Lets baselines pin "this was the prompt version that produced
    #: these outputs" for audit even without a full template registry.
    template_hash: str = ""


class EvalSummary(BaseModel):
    """Aggregate over a run's worth of cases."""
    total: int
    passed: int
    failed: int
    errored: int
    skipped: int = 0
    results: list[EvalResult] = Field(default_factory=list)

    @property
    def all_pass(self) -> bool:
        return self.failed == 0 and self.errored == 0 and self.passed == self.total


# ─── Dataset loading ────────────────────────────────────────────────


def load_dataset(path: Path) -> list[EvalCase]:
    """Read one ``*.jsonl`` file. Each line is one :class:`EvalCase`.

    Strict: a malformed line raises immediately. Eval authors want to
    know about typos at load time, not after running 99 cases.
    """
    out: list[EvalCase] = []
    with path.open() as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"{path}:{lineno}: invalid JSON: {e.msg}"
                ) from e
            try:
                out.append(EvalCase.model_validate(obj))
            except Exception as e:  # noqa: BLE001 — surface Pydantic detail
                raise ValueError(
                    f"{path}:{lineno}: invalid case shape — {e}"
                ) from e
    return out


def load_pack_datasets(evals_dir: Path) -> dict[str, list[EvalCase]]:
    """Discover every ``*.jsonl`` in ``evals_dir`` and load it.

    Returns a dict keyed by the dataset *stem* (filename without
    extension) so callers can filter with ``--dataset <name>``.
    """
    if not evals_dir.is_dir():
        return {}
    out: dict[str, list[EvalCase]] = {}
    for jl in sorted(evals_dir.glob("*.jsonl")):
        out[jl.stem] = load_dataset(jl)
    return out


# ─── Scoring ────────────────────────────────────────────────────────


def score_case(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Return a list of mismatch descriptions. Empty list = pass.

    Walks every leaf path in ``expected`` and compares to ``actual``.
    Nested dicts recurse; lists compare by index up to the shorter
    length (so expected can specify just the first N entries); scalars
    compare with ``==``.

    The output format is one human-readable line per mismatch:
        ``root_cause.category: expected 'deploy', got 'network'``
        ``matched_skills: expected 1, got 0``
        ``findings.evidence[2]: missing``
    """
    return _walk(expected, actual, path="")


def _walk(expected: Any, actual: Any, *, path: str) -> list[str]:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path or '<root>'}: expected dict, got {_typename(actual)}"]
        mismatches: list[str] = []
        for key, exp_val in expected.items():
            sub_path = f"{path}.{key}" if path else key
            if key not in actual:
                mismatches.append(f"{sub_path}: missing")
                continue
            mismatches.extend(_walk(exp_val, actual[key], path=sub_path))
        return mismatches

    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path or '<root>'}: expected list, got {_typename(actual)}"]
        mismatches = []
        # Compare up to the shorter length; expected can specify fewer.
        for i, exp_item in enumerate(expected):
            if i >= len(actual):
                mismatches.append(f"{path}[{i}]: missing")
                continue
            mismatches.extend(
                _walk(exp_item, actual[i], path=f"{path}[{i}]")
            )
        return mismatches

    # Scalar leaf
    if expected != actual:
        return [
            f"{path or '<root>'}: expected {expected!r}, got {actual!r}"
        ]
    return []


def _typename(v: Any) -> str:
    return type(v).__name__
