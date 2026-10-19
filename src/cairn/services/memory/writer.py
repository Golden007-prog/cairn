"""
Memory writer — turns a RunResult into an episodic memory record.

The Runner calls ``write_investigation_to_memory`` after each run (when the
skill has memory_enabled). The record is stored via
``cairn.services.memory.store.save_episodic``, which auto-deduplicates at
85% cosine similarity.

What gets saved:
  - service           — primary service involved (from the trigger)
  - error_type        — the matched pattern or alert source
  - summary           — one-line distillation of the RCA
  - root_cause        — extracted from the investigation output
  - resolution        — actions taken + their verified outcomes
  - severity          — derived from action risk levels
  - services_affected — primary service + anything in findings

Distillation strategy (in order, first non-empty wins):
  (a) Structured fields produced by a template's ``synthesize`` node
      (best — clean, deterministic)
  (b) Heuristic extraction from ``raw_output`` / ``findings`` dicts
  (c) Verbatim truncated ``raw_output['output']`` as summary, root_cause and
      resolution left empty (worst — but always works)
"""

from __future__ import annotations

import logging
from typing import Any

from cairn.core.engine.schema import ActionOutcome, RunResult

logger = logging.getLogger("cairn.memory_writer")


# Max length for fields we put into Qdrant — long enough to be useful,
# short enough to keep embeddings focused.
_MAX_SUMMARY = 240
_MAX_ROOT_CAUSE = 800
_MAX_RESOLUTION = 800


async def write_investigation_to_memory(result: RunResult) -> dict[str, Any]:
    """Save an investigation as an episodic memory.

    Returns the save_memory result (success / merged / error). Never raises;
    caller (Runner) logs failures but doesn't fail the investigation.
    """
    # Late import — the generic episodic store has optional deps (qdrant-client,
    # fastembed); degrade gracefully when not installed.
    try:
        from cairn.services.memory.store import save_episodic
    except ImportError as exc:
        return {"error": f"memory deps not available: {exc}"}

    service = result.trigger.service or "unknown"
    error_type = (
        result.trigger.matched_pattern
        or result.trigger.payload.get("alert_name")
        or result.trigger.payload.get("error_type")
        or "unknown"
    )[:120]

    summary = _distill_summary(result)[:_MAX_SUMMARY]
    root_cause = _distill_root_cause(result)[:_MAX_ROOT_CAUSE]
    resolution = _distill_resolution(result)[:_MAX_RESOLUTION]
    severity = _derive_severity(result.actions or [], result.action_outcomes or [])
    services_affected = _gather_affected_services(result, service)

    payload = {
        "service": service,
        "error_type": error_type,
        "summary": summary,
        "root_cause": root_cause,
        "resolution": resolution,
        "severity": severity,
        "services_affected": services_affected,
    }
    try:
        return await save_episodic(
            payload,
            embed_text=f"{service} {error_type} {summary} {root_cause}",
        )
    except Exception as exc:
        logger.warning("save_episodic failed: %s", exc)
        return {"error": str(exc)}


# ─── Distillation helpers ────────────────────────────────────────────


def _distill_summary(result: RunResult) -> str:
    """One-line summary. Prefer structured fields when present."""
    # (a) Structured RCA → headline
    if result.rca is not None and getattr(result.rca, "headline", None):
        return str(result.rca.headline)

    # (b) raw_output may carry a 'summary' / 'headline' field
    ro = result.raw_output or {}
    for key in ("headline", "summary", "title"):
        if isinstance(ro.get(key), str) and ro[key].strip():
            return ro[key].strip()

    # (c) Last resort: use the trigger symptom + service
    sym = result.trigger.matched_pattern or _first_line(
        result.trigger.payload.get("message") or ""
    )
    if sym:
        return f"{sym} on {result.trigger.service or 'unknown'}"
    return f"Run {result.skill_name} on {result.trigger.service or 'unknown'}"


def _distill_root_cause(result: RunResult) -> str:
    """Multi-sentence root-cause explanation."""
    # (a) Structured RCA
    if result.rca is not None and getattr(result.rca, "root_cause", None):
        return str(result.rca.root_cause)

    # (b) raw_output structured fields
    ro = result.raw_output or {}
    for key in ("root_cause", "cause", "diagnosis"):
        v = ro.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # (c) Sometimes the synthesize node output is the most informative
    findings = ro.get("findings") if isinstance(ro.get("findings"), dict) else {}
    for key in ("synthesize", "summary", "synthesis"):
        if isinstance(findings.get(key), str) and findings[key].strip():
            return findings[key].strip()

    # (d) Fallback: raw output text
    output_text = ro.get("output")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    return ""


def _distill_resolution(result: RunResult) -> str:
    """What was done about it. Built from action outcomes."""
    # (a) Structured RCA
    if result.rca is not None and getattr(result.rca, "resolution", None):
        return str(result.rca.resolution)

    # (b) From action outcomes — the most reliable post-hoc signal
    parts: list[str] = []
    for outcome in result.action_outcomes or []:
        verb = _action_outcome_verb(outcome)
        if verb:
            parts.append(verb)
    if parts:
        return "; ".join(parts)

    # (c) Otherwise list the proposed (but unexecuted) actions
    proposed = [
        f"proposed {ad.candidate.type} (decision={ad.decision.effect})"
        for ad in (result.actions or [])
    ]
    if proposed:
        return "; ".join(proposed)
    return ""


def _action_outcome_verb(outcome: ActionOutcome) -> str:
    """Human-readable one-liner per action outcome."""
    base = f"{outcome.action_type}"
    if outcome.status == "success":
        return f"{base}: succeeded"
    if outcome.status == "failed_execute":
        return f"{base}: execute failed"
    if outcome.status == "failed_verification":
        suffix = " (compensated)" if outcome.compensated else ""
        return f"{base}: verify failed{suffix}"
    if outcome.status == "rejected":
        return f"{base}: rejected by approver"
    if outcome.status == "denied":
        return f"{base}: denied by policy"
    if outcome.status == "skipped":
        return f"{base}: skipped (dry-run only)"
    if outcome.status == "error":
        return f"{base}: error"
    return f"{base}: {outcome.status}"


def _derive_severity(actions: list, outcomes: list[ActionOutcome]) -> str:
    """Crude severity inference from action risk levels + outcomes."""
    risk_levels = {ad.candidate.risk for ad in actions} if actions else set()
    if "high" in risk_levels:
        return "P1"
    if any(o.status in ("failed_execute", "failed_verification") for o in outcomes):
        return "P2"
    if "medium" in risk_levels:
        return "P2"
    if risk_levels or outcomes:
        return "P3"
    return ""


def _gather_affected_services(
    result: RunResult, primary: str
) -> list[str]:
    services = {primary}
    ro = result.raw_output or {}
    for key in ("services_affected", "downstream_services", "upstream_services"):
        v = ro.get(key)
        if isinstance(v, list):
            services.update(str(s) for s in v if s)
    services.discard("")
    services.discard("unknown")
    return sorted(services) or [primary]


def _first_line(s: str) -> str:
    return (s.splitlines()[0] if s else "").strip()
