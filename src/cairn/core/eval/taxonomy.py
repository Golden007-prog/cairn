"""Failure taxonomy — the diagnosis primitive (#474, epic #480).

ADR-0059 names this the schema for the "diagnose" leg of the closed loop
(``run -> evaluate -> type the failure -> cluster -> suggest -> human-gated
PR``): ``failure_labels(label_id PK, run_id FK, failure_type, classifier,
evidence JSON, created_at)``. This module is the **pure-function** half of
that primitive — it decides *which* :class:`FailureType` values apply to a
run, given the run record and its event log, and returns
:class:`FailureLabel` rows ready for a caller to persist.

Deliberately excluded (ADR-0012, "own the verdict, bind the scorers"):
  - No storage import. ``classify_failure`` takes plain ``dict`` shapes (the
    JSON a caller already has from ``RunStore``/``RunEvent.model_dump()``) and
    returns plain dataclasses — never touches ``core/storage`` or ``core/obs``
    directly, so this module is trivially unit-testable and safe to run
    offline/in a batch job (#477 continuous eval) without a live store.
  - No LLM calls. Every classifier here is a deterministic mapping from an
    observable field to a type, each carrying its evidence. The ambiguous
    "quality tail" that deterministic rules can't resolve is explicitly
    OUT of scope for this module — see :func:`classify_failure_llm` below,
    which is a stub for a future ticket, not an implementation.

Signal grounding (traced against the real shapes, not invented):
  - ``run["status"]`` / ``run["error"]`` — :class:`cairn.core.storage.protocols.Run`
    (``error`` is a plain ``str(exc)`` set by the dispatcher on any uncaught
    exception — see ``core/dispatch/dispatcher.py``).
  - event ``type``/``data`` — :class:`cairn.core.storage.protocols.RunEvent`.
    The concrete ``type``/``data`` shapes classified here come from
    ``core/engine/runner.py`` (``"findings"``, ``"action_decision"``,
    ``"action_outcome"``) and ``core/engine/schema.py`` (``PolicyDecision.effect``,
    ``ActionOutcome.status`` literals).
  - budget breach — ``run_within_budget`` in ``core/engine/runner.py`` returns
    ``{"type": "budget_exceeded", "budget_exceeded": True, "limit_wall_seconds": ...}``
    as the template's output (surfaced via the ``"findings"`` event's
    ``data.findings`` or a result payload's ``raw_output``); the *cost* budget
    is ``cairn.core.obs.cost.BudgetExceeded``, whose message reaches ``run.error``
    the same way any uncaught exception does.
  - retrieval — :class:`cairn.core.storage.protocols.RagStore`.search returns
    ``{"answer": str, "sources": list[dict]}``; an empty ``sources`` list on a
    run whose findings/output carries a ``sources`` key is the observable
    "retrieval came back empty" signal.
  - eval — :mod:`cairn.core.eval.scorer` (``EvalResult.status in {"error"}``),
    :mod:`cairn.core.eval.comparison` (``CaseComparison.verdict == "error"``),
    :mod:`cairn.core.eval.recipe` (``EvalRecipeResult.error`` /
    ``EvalRecipeResult.verdict == "fail"``).

Signals NOT cleanly available today are left as ``# TODO(#476): needs <field>``
comments at the relevant classifier rather than invented fields — #476
("capture") is the ticket that lands OpenInference span attrs / richer event
payloads this module can then key off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class FailureType(Enum):
    """The eight failure buckets the taxonomy classifies into (#474).

    A failed run can carry MULTIPLE labels (e.g. a tool timeout that also
    blew the cost budget is both TOOL_FAILURE and PERFORMANCE_FAILURE). A
    successful run gets none.
    """

    INPUT_FAILURE = "input_failure"
    RETRIEVAL_FAILURE = "retrieval_failure"
    GENERATION_FAILURE = "generation_failure"
    TOOL_FAILURE = "tool_failure"
    SYSTEM_FAILURE = "system_failure"
    SAFETY_FAILURE = "safety_failure"
    PERFORMANCE_FAILURE = "performance_failure"
    EVAL_FAILURE = "eval_failure"
    #: A run that finished mechanically but whose *result* was scored bad by
    #: the auto-eval hook (ADR-0060 §D1 / Open Decision #7) — a `fail`/
    #: `degraded` runtime `[eval.*]` verdict (low faithfulness, wrong schema,
    #: missed a gate). Distinct from EVAL_FAILURE (an eval *pipeline* error /
    #: judge disagreement classified from run/event shapes) so the drill-down
    #: cleanly separates "the run crashed" from "the run's output was judged
    #: bad". Emitted only by `cairn.core.eval.auto_eval`, never by
    #: `classify_failure`'s deterministic rules.
    QUALITY_FAILURE = "quality_failure"


@dataclass(frozen=True)
class FailureLabel:
    """One diagnosis: a failure type plus the evidence that justified it.

    Mirrors the ``failure_labels`` row shape from ADR-0059 D3
    (``label_id PK, run_id FK, failure_type, classifier, evidence JSON,
    created_at``) minus the storage-owned columns (``label_id``, ``run_id``,
    ``created_at``) — those are a caller's job when persisting, not this
    module's (no storage import here).
    """

    failure_type: FailureType
    #: ``"deterministic"`` (this module, today) or ``"llm"`` (future #476+).
    classifier: str
    #: References to the events/spans that justify the label, e.g.
    #: ``{"event_seq": 4, "event_type": "action_outcome", "reason": "..."}``.
    evidence: dict[str, Any] = field(default_factory=dict)


# ─── Keyword banks for string-matching plain error messages ───────────
#
# ``run["error"]`` is a bare ``str(exc)`` (no structured error code exists
# today — see the dispatcher). Keyword matching on it is best-effort by
# construction; a structured error taxonomy upstream would replace this.

_TIMEOUT_KEYWORDS = ("timeout", "timed out", "wall_seconds")
_RATE_LIMIT_KEYWORDS = ("rate limit", "rate_limit", "429", "too many requests")
_PROVIDER_OUTAGE_KEYWORDS = (
    "503", "502", "504", "service unavailable", "bad gateway",
    "connection error", "connection reset", "provider", "upstream",
)
_BUDGET_KEYWORDS = ("over budget", "budgetexceeded", "cost cap", "max_cost_usd")
_SAFETY_KEYWORDS = (
    "pii", "secret", "unsafe", "policy", "denied", "redact", "credential",
)


def _has_any(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in keywords)


def _events_of_type(events: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return [e for e in events if e.get("type") == event_type]


# ─── Deterministic classifiers ─────────────────────────────────────────
#
# Each ``_classify_*`` function inspects ``run``/``events`` for one family of
# signal and returns zero or more labels. ``classify_failure`` runs all of
# them and concatenates — a run can legitimately get multiple labels.


def _classify_system_and_performance_from_error(
    run: dict[str, Any],
) -> list[FailureLabel]:
    """SYSTEM_FAILURE / PERFORMANCE_FAILURE from ``run["error"]`` (dispatcher
    catch-all — ``core/dispatch/dispatcher.py``: ``error=str(exc)``).

    Timeout / provider outage / rate limit / orchestration error -> SYSTEM_FAILURE
    (or PERFORMANCE_FAILURE when the message signals pure slowness, i.e. a
    wall-clock timeout with no provider/outage signal).
    """
    error = run.get("error")
    if not error or not isinstance(error, str):
        return []

    labels: list[FailureLabel] = []
    is_timeout = _has_any(error, _TIMEOUT_KEYWORDS)
    is_rate_limit = _has_any(error, _RATE_LIMIT_KEYWORDS)
    is_outage = _has_any(error, _PROVIDER_OUTAGE_KEYWORDS)
    is_budget = _has_any(error, _BUDGET_KEYWORDS)

    if is_budget:
        labels.append(
            FailureLabel(
                failure_type=FailureType.PERFORMANCE_FAILURE,
                classifier="deterministic",
                evidence={"field": "run.error", "reason": "cost budget breach", "detail": error},
            )
        )
    elif is_timeout and not (is_rate_limit or is_outage):
        # Pure wall-clock slowness, no provider/rate-limit signal alongside it.
        labels.append(
            FailureLabel(
                failure_type=FailureType.PERFORMANCE_FAILURE,
                classifier="deterministic",
                evidence={"field": "run.error", "reason": "wall-clock timeout", "detail": error},
            )
        )
    elif is_timeout or is_rate_limit or is_outage:
        labels.append(
            FailureLabel(
                failure_type=FailureType.SYSTEM_FAILURE,
                classifier="deterministic",
                evidence={
                    "field": "run.error",
                    "reason": "timeout/provider-outage/rate-limit",
                    "detail": error,
                },
            )
        )
    elif run.get("status") == "failed":
        # A run failed but the message doesn't match a known category — still
        # a runtime/orchestration error (the dispatcher's generic catch),
        # distinct from the more specific buckets below.
        labels.append(
            FailureLabel(
                failure_type=FailureType.SYSTEM_FAILURE,
                classifier="deterministic",
                evidence={
                    "field": "run.error",
                    "reason": "unclassified orchestration error",
                    "detail": error,
                },
            )
        )
    return labels


def _classify_performance_from_budget_events(
    events: list[dict[str, Any]],
) -> list[FailureLabel]:
    """PERFORMANCE_FAILURE from a ``budget_exceeded`` template output.

    ``run_within_budget`` (``core/engine/runner.py``) returns
    ``{"type": "budget_exceeded", "budget_exceeded": True, "limit_wall_seconds": N}``
    as the template output on a wall-clock budget breach; this rides the
    ``"findings"`` event's ``data.findings`` (or a result payload's
    ``raw_output``, which callers may also pass in via ``events`` as a
    synthetic entry — this classifier only looks at ``events``).
    """
    labels: list[FailureLabel] = []
    for ev in _events_of_type(events, "findings"):
        findings = (ev.get("data") or {}).get("findings")
        if isinstance(findings, dict) and (
            findings.get("budget_exceeded") is True
            or findings.get("type") == "budget_exceeded"
        ):
            labels.append(
                FailureLabel(
                    failure_type=FailureType.PERFORMANCE_FAILURE,
                    classifier="deterministic",
                    evidence={
                        "event_seq": ev.get("seq"),
                        "event_type": "findings",
                        "reason": "template budget_exceeded (wall-clock)",
                        "limit_wall_seconds": findings.get("limit_wall_seconds"),
                    },
                )
            )
    return labels


def _classify_tool_failure(events: list[dict[str, Any]]) -> list[FailureLabel]:
    """TOOL_FAILURE from ``action_outcome`` events.

    ``ActionOutcome.status`` (``core/engine/schema.py``) carries
    ``"failed_execute"`` / ``"failed_verification"`` / ``"error"`` for a tool
    that raised, returned ``success=False``, or verify-failed after execute.
    ``"rejected"``/``"denied"`` are HITL/policy outcomes, not tool failures —
    excluded here (``"denied"`` is SAFETY_FAILURE territory, see below).
    """
    tool_fail_statuses = {"failed_execute", "failed_verification", "error"}
    labels: list[FailureLabel] = []
    for ev in _events_of_type(events, "action_outcome"):
        data = ev.get("data") or {}
        status = data.get("status")
        if status in tool_fail_statuses:
            labels.append(
                FailureLabel(
                    failure_type=FailureType.TOOL_FAILURE,
                    classifier="deterministic",
                    evidence={
                        "event_seq": ev.get("seq"),
                        "event_type": "action_outcome",
                        "reason": f"tool outcome status={status}",
                        "candidate_id": data.get("candidate_id"),
                        "action_type": data.get("action_type"),
                    },
                )
            )
    return labels


def _classify_safety_failure(events: list[dict[str, Any]]) -> list[FailureLabel]:
    """SAFETY_FAILURE from policy-gate blocks (and PII/unsafe keywords).

    ``PolicyDecision.effect == "deny"`` (``core/engine/schema.py``) surfaces on
    the ``action_decision`` event's ``data.effect``. The event payload
    (``core/engine/runner.py``) doesn't carry ``reasons`` today — only
    ``candidate_id``/``action_type``/``effect`` — so a deny is always flagged;
    when a richer ``reasons``/``matched_policies`` list IS present (e.g. a
    caller enriches events before calling this, or a future event schema adds
    it), PII/secret/unsafe keyword matches are called out in the evidence.

    # TODO(#476): needs a dedicated redaction-span event (RedactSpan.kind —
    # "email"/"credit_card"/"aws_key" — from core/obs/redact.py) to detect a
    # PII *leak* rather than just a policy *deny*; today's redaction chokepoint
    # doesn't emit a RunEvent, so a leak that wasn't gated by a deny-effect
    # policy is invisible to this classifier.
    """
    labels: list[FailureLabel] = []
    for ev in _events_of_type(events, "action_decision"):
        data = ev.get("data") or {}
        if data.get("effect") == "deny":
            reasons = data.get("reasons") or []
            reason_text = " ".join(str(r) for r in reasons) if reasons else ""
            is_pii_or_unsafe = bool(reason_text) and _has_any(reason_text, _SAFETY_KEYWORDS)
            labels.append(
                FailureLabel(
                    failure_type=FailureType.SAFETY_FAILURE,
                    classifier="deterministic",
                    evidence={
                        "event_seq": ev.get("seq"),
                        "event_type": "action_decision",
                        "reason": "policy gate denied"
                        + (" (pii/unsafe keyword match)" if is_pii_or_unsafe else ""),
                        "candidate_id": data.get("candidate_id"),
                        "action_type": data.get("action_type"),
                        "matched_reasons": reasons,
                    },
                )
            )
    return labels


def _classify_retrieval_failure(events: list[dict[str, Any]]) -> list[FailureLabel]:
    """RETRIEVAL_FAILURE — empty ``sources`` where retrieval ran.

    :class:`cairn.core.storage.protocols.RagStore`.search returns
    ``{"answer": str, "sources": list[dict]}``; a present-but-empty
    ``sources`` list (as opposed to the key being entirely absent, which
    means retrieval was never invoked for this event) is the observable
    "retrieval came back empty" signal. Looked for in ``findings`` events'
    ``data.findings`` dict — the same place a RAG-backed skill's output
    surfaces (mirrors ``_classify_performance_from_budget_events`` above).
    """
    labels: list[FailureLabel] = []
    for ev in _events_of_type(events, "findings"):
        findings = (ev.get("data") or {}).get("findings")
        if isinstance(findings, dict) and "sources" in findings:
            sources = findings.get("sources")
            if isinstance(sources, list) and len(sources) == 0:
                labels.append(
                    FailureLabel(
                        failure_type=FailureType.RETRIEVAL_FAILURE,
                        classifier="deterministic",
                        evidence={
                            "event_seq": ev.get("seq"),
                            "event_type": "findings",
                            "reason": "retrieval expected (sources key present) but empty",
                        },
                    )
                )
    return labels


def _classify_input_failure(run: dict[str, Any]) -> list[FailureLabel]:
    """INPUT_FAILURE — invalid/missing input on the run record.

    ``Run.inputs`` (``core/storage/protocols.py``) is the validated input the
    run launched with; an empty dict on a run that nonetheless failed is a
    plausible "no usable input reached the run" signal. This is
    conservative-by-design: a template that legitimately takes no inputs
    would also have an empty dict, so this only fires when the run also
    failed (paired with ``status``), never on a successful empty-input run.

    # TODO(#476): needs a structured input-validation-error signal (e.g. a
    # pydantic ValidationError raised at trigger/schema-validation time,
    # surfaced as its own RunEvent type) to distinguish "no inputs needed"
    # from "inputs were rejected" without the empty-dict heuristic above.
    """
    if run.get("status") != "failed":
        return []
    inputs = run.get("inputs")
    if inputs is not None and len(inputs) == 0:
        return [
            FailureLabel(
                failure_type=FailureType.INPUT_FAILURE,
                classifier="deterministic",
                evidence={
                    "field": "run.inputs",
                    "reason": "run failed with no recorded input values",
                },
            )
        ]
    return []


def _classify_generation_failure(events: list[dict[str, Any]]) -> list[FailureLabel]:
    """GENERATION_FAILURE — schema-invalid / json-parse-fail / instruction-ignored.

    # TODO(#476): needs a structured signal. Today, a template's LLM output
    # that fails JSON-parse or schema-validation is either (a) caught inside
    # the template/operator and folded into its own output shape (not a
    # RunEvent type this module can key off), or (b) propagates as a bare
    # exception into ``run.error`` indistinguishable from any other runtime
    # error. Once #476 lands a structured event (e.g. ``type="generation_error"``
    # with ``data.reason in {"json_parse", "schema_invalid", "instruction_ignored"}``),
    # wire it here. Left unimplemented rather than pattern-matching ``run.error``
    # strings, which would be indistinguishable from SYSTEM_FAILURE guesses.
    """
    return []


def _classify_eval_failure(run: dict[str, Any], events: list[dict[str, Any]]) -> list[FailureLabel]:
    """EVAL_FAILURE — judge disagreement / invalid eval input.

    Grounded in the eval module's real result shapes:
      - :class:`cairn.core.eval.scorer.EvalResult` — ``status == "error"``
        (pipeline raised; couldn't score) carried on the run dict as
        ``eval_result`` (a caller-attached projection — see the "eval-shaped"
        note in the test fixtures) or as an ``eval_result`` event.
      - :class:`cairn.core.eval.comparison.CaseComparison` — ``verdict == "error"``.
      - :class:`cairn.core.eval.recipe.EvalRecipeResult` — ``error`` set, or
        ``verdict == "fail"`` (a ``model/gate``-style judge fail = disagreement
        with the expected outcome).
    Accepts either an ``eval_result``/``eval_recipe_result``/``comparison`` key
    directly on ``run`` (a lightweight caller convention, no storage coupling)
    or an ``"eval_result"`` event in ``events`` carrying the same dict shape.
    """
    labels: list[FailureLabel] = []

    candidates: list[tuple[str, dict[str, Any]]] = []
    for key in ("eval_result", "eval_recipe_result", "comparison"):
        value = run.get(key)
        if isinstance(value, dict):
            candidates.append((f"run.{key}", value))
    for ev in _events_of_type(events, "eval_result"):
        data = ev.get("data") or {}
        candidates.append((f"event(seq={ev.get('seq')}).data", data))

    for source, data in candidates:
        status = data.get("status")
        verdict = data.get("verdict")
        error = data.get("error")
        if status == "error" or verdict == "error":
            labels.append(
                FailureLabel(
                    failure_type=FailureType.EVAL_FAILURE,
                    classifier="deterministic",
                    evidence={"field": source, "reason": "eval pipeline errored / invalid eval input"},
                )
            )
        elif verdict == "fail" or error:
            labels.append(
                FailureLabel(
                    failure_type=FailureType.EVAL_FAILURE,
                    classifier="deterministic",
                    evidence={
                        "field": source,
                        "reason": "eval judge disagreement" if verdict == "fail" else "eval recipe error",
                        "detail": error,
                    },
                )
            )
    return labels


# ─── Public entry point ────────────────────────────────────────────────


def classify_failure(run: dict[str, Any], events: list[dict[str, Any]]) -> list[FailureLabel]:
    """Deterministically classify a run's failure(s) from its record + events.

    Pure function: ``run`` is a plain dict shaped like
    :class:`cairn.core.storage.protocols.Run`.model_dump() (or a subset with
    at least ``status``/``error``/``inputs``); ``events`` is a list of plain
    dicts shaped like :class:`cairn.core.storage.protocols.RunEvent`.model_dump()
    (at least ``type``/``data``/``seq``). No storage import, no I/O.

    A run may accumulate labels from MULTIPLE classifiers (e.g. a tool
    timeout that also breached the cost budget gets both TOOL_FAILURE and
    PERFORMANCE_FAILURE). A successful run (``status`` not ``"failed"`` and
    no event-level failure signals) returns an empty list.

    This runs the **deterministic** classifiers only (ADR-0059 D6: "free
    gates first"). The LLM classifier for the ambiguous quality tail is
    :func:`classify_failure_llm` — not called from here.
    """
    labels: list[FailureLabel] = []
    labels.extend(_classify_system_and_performance_from_error(run))
    labels.extend(_classify_performance_from_budget_events(events))
    labels.extend(_classify_tool_failure(events))
    labels.extend(_classify_safety_failure(events))
    labels.extend(_classify_retrieval_failure(events))
    labels.extend(_classify_input_failure(run))
    labels.extend(_classify_generation_failure(events))
    labels.extend(_classify_eval_failure(run, events))
    return labels


def classify_failure_llm(
    run: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    enabled: bool = False,
) -> list[FailureLabel]:
    """STUB — future LLM classifier for the ambiguous quality tail (#480 follow-on).

    ADR-0059 D6's ordering principle ("deterministic gates first (free) ->
    LLM-judge only for the quality tail") applies here too: this is the
    LLM-judge leg of the failure taxonomy, for cases the deterministic rules
    in :func:`classify_failure` can't resolve (e.g. "the output is
    schema-valid and no gate fired, but a human would call this a bad
    generation"). NOT implemented — no LLM call happens in this module.

    Design intent (not yet built):
      - Runs ONLY over the residual — runs/events that ``classify_failure``
        returned no labels for (or an explicit "ambiguous" marker), never as
        a replacement for the free deterministic pass.
      - Must still return ``FailureLabel`` rows with ``classifier="llm"`` and
        an ``evidence`` dict citing the specific judge call (model, prompt
        version, score) — same audit bar as every other governed operator
        (ADR-0012: suggested/labeled, never silently authoritative).
      - Belongs in the operator layer (a real ``eval/judge``-style operator,
        cost-tracked and policy-gated), not inlined here — this module stays
        storage-free and dependency-free per the module docstring.

    Raises:
        NotImplementedError: always, when ``enabled`` is True (an explicit
        opt-in that still isn't wired to anything). When ``enabled`` is
        False (the default), this is a flag-gated no-op that returns ``[]``
        so a caller can wire the call site today without it ever firing.
    """
    if not enabled:
        return []
    raise NotImplementedError(
        "LLM failure classification is not implemented (#480 follow-on). "
        "classify_failure() covers all deterministic rules; this stub exists "
        "only to reserve the call site."
    )


__all__ = [
    "FailureType",
    "FailureLabel",
    "classify_failure",
    "classify_failure_llm",
]
