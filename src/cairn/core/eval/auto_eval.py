"""Auto-eval on completion (#480, ADR-0060 §D1) — the sibling of failure-labeling.

``cairn.core.eval.diagnosis`` wires the taxonomy (#474) into the eval ledger for
the *mechanical* failure path. This module wires the *quality* path: it relocates
the exact sequence the on-demand ``POST /runs/{id}/evaluate`` endpoint already
runs (``resolve_runtime_contract`` -> ``extract_run_output`` -> ``run_eval_recipe``
-> ``record_eval_run``, all in ``recipe.py``/``eval_ledger.py``) into the
dispatcher's completion hook, so every terminal run gets its declared
``[eval.*]`` CONTRACT scored automatically — every HARD gate runs, the verdicts
aggregate (any gate ``fail`` ⇒ run ``fail``), and case results are tagged with
the contract's ``slice_by`` dimensions (#487) — a verdict + slices land in the
ledger without a human ever clicking "Evaluate this result". Objectives are
parse-only until #458 (Phase 6).

Like :func:`cairn.core.eval.diagnosis.label_run_failures`, this is a best-effort
seam: any exception (no recipe, bad selector, judge outage, ledger hiccup) is
caught, logged, and swallowed — auto-eval must never break a run's completion.

**Cost control (ADR-0060 §D1 table):**
  - deterministic recipes (e.g. ``model/gate``) always run — they're free.
  - LLM-judge-backed recipes (operators declaring ``eval.judge``) are SAMPLED:
    ``CAIRN_AUTOEVAL_JUDGE_SAMPLE_PCT`` of runs (default 0.2), deterministic per
    ``run_id`` (a stable hash, not RNG, so it's reproducible/testable) — UNLESS
    the run already carries a failure label, in which case it always runs
    (score the tail we care about at 100%, sample the healthy body).
  - ``CAIRN_AUTOEVAL=off`` is a per-env kill switch for the whole step.

**Quality-failure label (closes the D1 hole):** a ``fail``/``degraded`` verdict
also emits a ``QUALITY_FAILURE`` label via ``ledger.add_failure_labels`` — so a
mechanically-successful run with a bad *result* enters the same drill-down/
clustering path a crash does (ADR-0060 §D1 / Open Decision #7).
"""

from __future__ import annotations
from cairn.core.settings import getenv

import hashlib
import logging
import os
from typing import Any, Optional

from cairn.core.eval.diagnosis import to_storage_label
from cairn.core.eval.taxonomy import FailureLabel as TaxonomyFailureLabel
from cairn.core.eval.taxonomy import FailureType
from cairn.core.storage.eval_ledger import EvalCaseResult, EvalRun

_log = logging.getLogger("cairn.eval.auto_eval")

#: Default judge-sample rate (ADR-0060 §D1 Open Decision #1's recommendation).
_DEFAULT_JUDGE_SAMPLE_PCT = 0.2

#: The capability a deliberately non-deterministic, LLM-judge-backed eval
#: operator declares (``[operator."<ref>".capabilities].provides`` in its
#: ``pack.toml``, mirrored by ``register_operator(capabilities=…)``). Every other
#: eval recipe operator is a pure/deterministic function of its inputs. Recipe
#: shapes carry no "deterministic vs judge" flag (ADR-0057's ``EvalRecipe`` is
#: just ``uses`` + ``inputs``), so the operator's declared capability is the one
#: place the distinction is knowable — read through the operator registry, so
#: core names no eval pack's operator (ADR-0120 D5, #2415).
_JUDGE_CAPABILITY = "eval.judge"


def _judge_backed(uses: str, registry: Any = None) -> bool:
    """True when the recipe operator ``uses`` declares :data:`_JUDGE_CAPABILITY`.
    An unregistered operator is not judge-backed (its recipe errors on its own)."""
    from cairn.core.operator.loader import ensure_loaded  # noqa: PLC0415 - keep the operator stack off import time
    from cairn.core.operator.registry import default_registry  # noqa: PLC0415

    reg = registry if registry is not None else default_registry
    ensure_loaded(registry=reg)
    if not reg.has(uses):
        return False
    return _JUDGE_CAPABILITY in (getattr(reg.get(uses), "capabilities", None) or ())


def _autoeval_enabled() -> bool:
    return getenv("CAIRN_AUTOEVAL", "on").strip().lower() != "off"


def _judge_sample_pct() -> float:
    raw = getenv("CAIRN_AUTOEVAL_JUDGE_SAMPLE_PCT", "").strip()
    if not raw:
        return _DEFAULT_JUDGE_SAMPLE_PCT
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_JUDGE_SAMPLE_PCT


def _sample_judge(run_id: str, pct: float) -> bool:
    """Deterministic per-run_id sample decision (reproducible/testable): a
    stable hash of ``run_id`` mod 100, compared against ``pct*100``."""
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    bucket = int(digest, 16) % 100
    return bucket < pct * 100


def _verdict_str(result: Any) -> str:
    """Normalize an ``EvalRecipeResult`` to a single pass/fail/degraded string
    (mirrors the ``/evaluate`` endpoint's own normalization, server.py)."""
    if result.verdict:
        return result.verdict
    if result.passed is not None:
        return "pass" if result.passed else "fail"
    return ""


def _aggregate_verdict(verdicts: list[str]) -> str:
    """Combine per-gate verdicts into the run's aggregate (#487, ADR-0060 D2):
    any hard-gate ``fail`` ⇒ ``fail``; else any ``warn``/``degraded`` ⇒
    ``degraded``; else all ``pass`` ⇒ ``pass``. ``""`` when nothing scored."""
    if not verdicts:
        return ""
    if any(v == "fail" for v in verdicts):
        return "fail"
    if any(v in ("warn", "degraded") for v in verdicts):
        return "degraded"
    if all(v == "pass" for v in verdicts):
        return "pass"
    return verdicts[0]


def _lookup(d: Any, dotted: str) -> Any:
    """Walk a dotted path into nested dicts; ``None`` on any miss/non-dict."""
    cur = d
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _extract_slices(
    run: dict[str, Any], output: dict[str, Any], slice_by: tuple[str, ...]
) -> dict[str, Any]:
    """Resolve the contract's ``slice_by`` dimension names to primitive values
    off the run (#487) — checked in output → inputs → metadata order. Only
    primitives (str/int/float/bool) are kept; a dimension that resolves to a
    container or is absent is simply omitted. Captured at eval time (immutable),
    so the slice is provenance, not a re-derivation over possibly-mutated inputs."""
    if not slice_by:
        return {}
    inputs = run.get("inputs") or {}
    meta = run.get("metadata") or {}
    out: dict[str, Any] = {}
    for dim in slice_by:
        val = _lookup(output, dim)
        if val is None:
            val = _lookup(inputs, dim)
        if val is None:
            val = _lookup(meta, dim)
        if isinstance(val, (str, int, float, bool)):
            out[dim] = val
    return out


def _result_reason(result: Any) -> Optional[str]:
    """Best textual reason a gate result carries (judge/summary → error → skip)."""
    if result is None:
        return None
    out = result.output or {}
    reason = None
    if isinstance(out, dict):
        reason = out.get("reason") or out.get("summary")
    return reason or result.error or result.skipped_reason


def _compose_summary(
    verdict: str, gate_lines: list[str], failing_result: Any
) -> str:
    """The #499 human-readable eval result: aggregate verdict + per-gate scores
    + the failing gate's reason. Empty when nothing scored (caller leaves it None)."""
    if not gate_lines:
        return ""
    head = f"Auto-eval verdict: {verdict or 'unknown'} — {len(gate_lines)} gate(s) scored."
    body = " " + "; ".join(gate_lines) + "."
    reason = _result_reason(failing_result) if verdict in ("fail", "degraded") else None
    tail = f" Reason: {reason}." if reason else ""
    return head + body + tail


def _quality_failure_label(
    run_id: str, recipe_uses: str, verdict: str, result: Any
) -> TaxonomyFailureLabel:
    """Build the QUALITY_FAILURE label for a bad (fail/degraded) verdict
    (ADR-0060 §D1 / Open Decision #7 — a distinct type, not folded into the
    mechanical GENERATION_FAILURE, so the drill-down separates "crashed" from
    "judged bad")."""
    reason = None
    if result is not None:
        out = result.output or {}
        if isinstance(out, dict):
            reason = out.get("reason") or out.get("summary")
        if not reason:
            reason = result.error or result.skipped_reason
    return TaxonomyFailureLabel(
        failure_type=FailureType.QUALITY_FAILURE,
        classifier="eval",
        evidence={
            "source": "eval_objective",
            "verdict": verdict,
            "recipe": recipe_uses,
            "reason": reason,
        },
    )


async def auto_eval_run(run: dict[str, Any], *, ledger: Any) -> Optional[str]:
    """Score ``run``'s runtime ``[eval.*]`` recipe on completion, best-effort.

    ``run`` is a plain dict shaped like ``Run.model_dump(mode="json")`` (the
    same shape :func:`cairn.core.eval.diagnosis.label_run_failures` takes).
    ``ledger`` is the eval-ledger store to write to — pass the store that OWNS
    the run (e.g. the platform ``scoring`` run hook's ledger), mirroring
    :func:`label_run_failures`'s own contract so labels and verdicts co-locate.

    Returns the normalized verdict string (``"pass"``/``"fail"``/``"degraded"``/
    ``""``) on success, or ``None`` when nothing was scored (kill switch, no
    recipe, sampled-out, or any failure) — the caller (dispatcher) uses a
    non-``None`` return to persist ``runs.final_verdict``.

    Best-effort by the same contract as ``label_run_failures``: any exception
    (no recipe, bad selector, judge outage, ledger hiccup) is caught, logged as
    a warning, and swallowed — this must never break a run's completion.
    """
    run_id = run.get("id", "")
    try:
        if not _autoeval_enabled():
            return None

        # lazy: keep the eval stack (operator registry, recipe resolution) off
        # the dispatcher's import-time path, mirroring server.py's own
        # /runs/{id}/evaluate handler.
        from cairn.core.eval.recipe import (
            extract_run_output,
            resolve_runtime_contract,
            run_eval_recipe,
        )

        template_ref = run.get("template_ref") or ""
        if not template_ref:
            # Callers that don't pre-resolve template_ref onto the run dict
            # (the dispatcher does, mirroring server.py's _run_template_ref)
            # get no-op'd rather than guessing at a ref.
            return None

        found = resolve_runtime_contract(template_ref)
        if not found:
            return None  # workflow declares no runtime [eval.*] contract
        contract, root = found
        if not contract.gates:
            # A slices/objectives-only contract (no hard gates) has nothing to
            # score yet — objectives are parse-only until #458 (Phase 6).
            return None

        payloads = run.get("result_payloads") or []
        output = extract_run_output(payloads)
        slices = _extract_slices(run, output, contract.slice_by)

        eval_run = EvalRun(
            run_id=run_id,
            recipe="eval/contract",
            tier="",
            judge_meta={"autoeval": True, "gates": [g.uses for g in contract.gates]},
        )

        # Score every HARD gate. Judge-backed gates are sampled (cost control):
        # unless the run already carries a failure label, an LLM-judge gate runs
        # only for the sampled fraction — deterministic gates always run (free).
        # `existing_labels` is fetched at most once, lazily.
        existing_labels: Optional[list] = None
        gate_verdicts: list[str] = []
        case_results: list[EvalCaseResult] = []
        agg_score: Optional[float] = None
        judge_any = False
        failing_result: Any = None
        gate_lines: list[str] = []  # #499 per-gate result text

        for gate in contract.gates:
            if _judge_backed(gate.uses):
                judge_any = True
                if existing_labels is None:
                    existing_labels = await ledger.get_failure_labels(run_id)
                already_failed = bool(existing_labels)
                if not already_failed and not _sample_judge(run_id, _judge_sample_pct()):
                    continue  # sampled out — skip this judge gate this run

            result = await run_eval_recipe(gate, output, pack_root=root)
            v = _verdict_str(result)
            if v:
                gate_verdicts.append(v)
            if v in ("fail", "warn", "degraded") and failing_result is None:
                failing_result = result
            score = (result.output or {}).get("score")
            if isinstance(score, (int, float)) and agg_score is None:
                agg_score = score
            line = f"{gate.uses} → {v or '—'}"
            if isinstance(score, (int, float)):
                line += f" ({score:.2f})"
            gate_lines.append(line)
            case_results.append(
                EvalCaseResult(
                    eval_run_id=eval_run.eval_run_id,
                    case_id=gate.uses,
                    slice=slices,
                    passed=(v == "pass"),
                    score=score if isinstance(score, (int, float)) else None,
                )
            )

        if not gate_verdicts and not case_results:
            # every gate was sampled out — nothing scored, nothing to persist.
            return None

        verdict_str = _aggregate_verdict(gate_verdicts)
        summary = _compose_summary(verdict_str, gate_lines, failing_result)
        eval_run = eval_run.model_copy(
            update={
                "verdict": verdict_str,
                "aggregate_score": agg_score,
                "summary": summary or None,
            }
        )
        if judge_any:
            eval_run.judge_meta["judge_sampled"] = True

        await ledger.record_eval_run(eval_run, case_results or None)

        if verdict_str in ("fail", "degraded"):
            label = _quality_failure_label(
                run_id, "eval/contract", verdict_str, failing_result
            )
            await ledger.add_failure_labels(run_id, [to_storage_label(label, run_id)])

        return verdict_str or None
    except Exception:  # noqa: BLE001 — best-effort, never break run completion
        _log.warning("auto_eval_run: failed to score run %s", run_id or "<unknown>", exc_info=True)
        return None


__all__ = ["auto_eval_run"]
