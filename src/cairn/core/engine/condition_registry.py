"""
Condition registry for GuardBTNode.

A condition function takes the current state dict and returns a string key
(typically 'true'/'false') that the guard uses to route. Functions are
registered globally via the @condition decorator.

Built-in conditions are defined at the bottom of this module.
"""

from __future__ import annotations

from typing import Callable

ConditionFn = Callable[[dict], str]

_REGISTRY: dict[str, ConditionFn] = {}


def condition(name: str | None = None):
    """Decorator that registers a function as a named guard condition."""
    def wrap(fn: ConditionFn) -> ConditionFn:
        key = name or fn.__name__
        _REGISTRY[key] = fn
        return fn
    return wrap


def resolve_condition(name: str) -> ConditionFn:
    if name not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY.keys()))
        raise KeyError(f"Unknown condition '{name}'. Known: {known}")
    return _REGISTRY[name]


def known_conditions() -> list[str]:
    return sorted(_REGISTRY.keys())


# ─── Built-in conditions ────────────────────────────────────────────


def _truthy(val) -> str:
    """Normalize any value to 'true' / 'false' route keys."""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, str):
        return "true" if val.lower() in ("true", "yes", "1") else "false"
    return "true" if val else "false"


@condition("classified_as_database")
def classified_as_database(state: dict) -> str:
    classification = (state.get("findings") or {}).get("classify", "")
    return _truthy("database" in classification.lower() or "db" in classification.lower())


@condition("classified_as_network")
def classified_as_network(state: dict) -> str:
    classification = (state.get("findings") or {}).get("classify", "")
    return _truthy("network" in classification.lower())


@condition("classified_as_application")
def classified_as_application(state: dict) -> str:
    classification = (state.get("findings") or {}).get("classify", "")
    return _truthy(
        "application" in classification.lower() or "app" in classification.lower()
    )


@condition("needs_more_evidence")
def needs_more_evidence(state: dict) -> str:
    """Heuristic: drill deeper if we have fewer than 3 evidence items."""
    evidence = state.get("evidence") or []
    return _truthy(len(evidence) < 3)


@condition("confidence_above_threshold")
def confidence_above_threshold(state: dict) -> str:
    """Used to skip extra synthesis if confidence is already high."""
    confidence = (state.get("findings") or {}).get("confidence", 0.0)
    try:
        return _truthy(float(confidence) >= 0.8)
    except (TypeError, ValueError):
        return "false"


@condition("last_action_succeeded")
def last_action_succeeded(state: dict) -> str:
    """Used by selectors to check if the previous branch's action succeeded."""
    return _truthy(state.get("last_action_status") == "success")


@condition("dataset_gate_passed")
def dataset_gate_passed(state: dict) -> str:
    """#9/#580: ENFORCE the dataset-readiness gate — only train when it didn't
    FAIL. `pass`/`warn`/absent (an ungated train template) all route to `then`
    (train); an explicit `fail` routes to `else` (skip training). Reads the
    ``findings`` merge channel the composed dataset subgraph threads its verdict
    on (#600), so it works under flatten + Temporal.

    #914 fail-CLOSED: a MISSING verdict is only "ungated by design" when the
    gate never ran. When the composed gate op ran and ERRORED (its
    ``run-gate_error`` failure-finding is in state — e.g. every check op blew up
    on an unreadable dataset, #913), nobody scanned the data — that must route
    to `else`, not sail into stage→cost→train. Found live: the #888 E2E run
    trained on a dataset whose every readiness op had errored."""
    findings = state.get("findings") or {}
    verdict = str(findings.get("dataset_verdict", "") or "").lower()
    if not verdict and findings.get("run-gate_error"):
        return _truthy(False)  # the gate itself errored → no verdict ⇒ BLOCK
    return _truthy(verdict != "fail")


@condition("prep_requested")
def prep_requested(state: dict) -> str:
    """#726: opt-in dataset PREP front-stage. True when the run set
    ``prep_enabled`` — then finetune runs anonymize→split and trains on the
    scrubbed TRAIN split (the held-out TEST split is kept for the significance
    gate). Off/absent → the flagship's existing raw-dataset stage runs unchanged,
    so a run that doesn't opt in is byte-for-byte the proven flow. Read from
    ``template_input`` (the verbatim input channel — a top-level key isn't a
    declared channel and would be dropped), mirroring ``notion_page_id_provided``."""
    ti = state.get("template_input") or {}
    return _truthy(bool(ti.get("prep_enabled")))


@condition("curate_requested")
def curate_requested(state: dict) -> str:
    """#926: opt-in LLM-judge CURATION front-stage. True when the run set
    ``curate_enabled`` — then finetune scores each example for quality and keeps
    only those clearing the bar (dataset/curate: AlpaGasus / DEITA) BEFORE staging
    or prep, and trains on the curated set (threaded via ``findings.curated_path``,
    which the stage + prep ops fall back to). Off/absent → nothing runs and the
    dataset is byte-for-byte the proven flow. Read from ``template_input`` (the
    verbatim input channel — a top-level key isn't a declared channel and would be
    dropped), mirroring ``prep_requested``."""
    ti = state.get("template_input") or {}
    return _truthy(bool(ti.get("curate_enabled")))


@condition("prep_scrub_gate_passed")
def prep_scrub_gate_passed(state: dict) -> str:
    """#882 — the POST-SCRUB PII gate (dataset/prep_gate). Routes `true` (proceed)
    UNLESS the gate emitted ``action=block`` (mode=block AND prep's re-scan found
    residual PII). A consumer/pipeline gates its train subtree on this exactly like
    ``dataset_gate_passed``. Absent (gate didn't run, or mode=off/hitl) → `true`, so
    a run that isn't hard-blocking proceeds. Reads the ``findings`` merge channel so
    it holds under flatten + Temporal.

    #914 fail-CLOSED: when the prep_gate op ran and ERRORED (``run-prep-gate_error``
    failure-finding present, no action written), the re-scan's verdict is unknown —
    route to block, don't proceed on a scrub nobody verified."""
    findings = state.get("findings") or {}
    action = str(findings.get("prep_gate_action", "") or "").lower()
    if not action and findings.get("run-prep-gate_error"):
        return _truthy(False)  # the gate itself errored → BLOCK (#914)
    return _truthy(action != "block")


@condition("prep_scrub_needs_review")
def prep_scrub_needs_review(state: dict) -> str:
    """#883 — route to the HITL review node ONLY when dataset/prep_gate emitted
    ``action=review`` (mode=hitl AND prep's re-scan found residual PII). Every
    other case (clean, mode=off, mode=block, gate absent) → `false`, so the review
    node — and its durable pause — never fires. Reads the ``findings`` merge
    channel so it holds under flatten + Temporal."""
    action = str((state.get("findings") or {}).get("prep_gate_action", "") or "").lower()
    return _truthy(action == "review")


@condition("watch_signal_crossed")
def watch_signal_crossed(state: dict) -> str:
    """#992 (ADR-0076 D4) — route to the human-gated proposal ONLY when the watch's
    monitor/threshold detector flagged ``crossed`` (the signal breached its
    threshold). Not crossed / detector absent → `false`, so the proposal node — and
    its durable approval pause — never fires and the watch simply records "no drift".
    Reads the ``findings`` merge channel so it holds under flatten + Temporal."""
    return _truthy(bool((state.get("findings") or {}).get("watch_crossed")))


@condition("watch_approved")
def watch_approved(state: dict) -> str:
    """#1162 — route to the DISPATCH step only when a human approved the proposal.

    `monitor/propose` sits behind a durable approval gate, so reaching it with
    ``watch_approved`` true means a person authorised the spend. Rejected, or the
    signal never crossed, → `false`, and the watch ends having spent nothing.
    Reads the ``findings`` merge channel so it holds under flatten + Temporal,
    exactly like `watch_signal_crossed`."""
    return _truthy(bool((state.get("findings") or {}).get("watch_approved")))


@condition("regression_baseline_provided")
def regression_baseline_provided(state: dict) -> str:
    """#895: gate the baseline-regression step (run-eval-delta) on a baseline being
    supplied. True only when the run gave a NON-EMPTY ``baseline_benchmark_metrics``
    (the prior promoted model's scores to regress-check against). Absent/empty →
    eval/delta does NOT run, so ``findings.delta_verdict`` stays unset and the model
    gate skips it (``$default(…, "")``) — the flagship's promote verdict is
    byte-for-byte unchanged for the common no-baseline run. Without this guard,
    eval/delta on an empty baseline returns `warn` ("nothing comparable — never a
    silent pass"), which would degrade EVERY default run's model_verdict to warn.
    Read from ``template_input`` (the verbatim input channel — a top-level key isn't
    a declared channel and would be dropped), mirroring ``prep_requested``."""
    ti = state.get("template_input") or {}
    return _truthy(bool(ti.get("baseline_benchmark_metrics")))


@condition("serve_requested")
def serve_requested(state: dict) -> str:
    """#726: opt-in SERVE-in-loop. True only when the run set ``serve_enabled``
    AND the model was actually promoted (``findings.model_registered``) — you never
    stand up a vLLM endpoint for a model that failed the gate or wasn't registered.
    Off/absent → no serve step (flagship unchanged). The ``serve_enabled`` flag is
    read from ``template_input``; the registration fact from the ``findings`` merge
    channel the promote step wrote to (#600), so it holds under flatten + Temporal."""
    ti = state.get("template_input") or {}
    registered = bool((state.get("findings") or {}).get("model_registered"))
    return _truthy(bool(ti.get("serve_enabled")) and registered)


@condition("notion_page_id_provided")
def notion_page_id_provided(state: dict) -> str:
    """True when the run was given a ``notion_page_id`` (#570). Read from
    ``template_input`` (the verbatim input channel) — NOT a top-level state key,
    which isn't a declared channel and would be dropped. Lets a template gate an
    opt-in Notion publish so it (and its external-write approval) only fires when
    there is actually a page to write to — and stays skipped when the template is
    composed as a subgraph without a notion_page_id mapped in."""
    ti = state.get("template_input") or {}
    return _truthy(bool(ti.get("notion_page_id") or state.get("notion_page_id")))
