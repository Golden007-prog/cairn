"""ApprovalResolver — compute the effective HITL gate for an operation (ADR-0034).

The gate is **non-bypassable**: authority comes from the DEPLOYMENT (profile +
policy), keyed on the operator-author's `pack.toml` `risk` + `capabilities`, NOT
from the domain pack. A pack's `OperationBinding.approval` is **strictness-only**
— it can turn a gate ON or shorten the timeout, never relax one a deployment
mandated. This mirrors ADR-0028's audit/redact ("optional to turn ON, not
optional to ESCAPE").

This module is a PURE decision function — it does no graph wiring, no interrupt,
no I/O. The runtime calls `resolve_gate(...)` at the operation-invocation
chokepoint and refuses to dispatch a required gate without an `ApprovalRecord`.

Four bypass vectors, all closed here:
  1. omission        — a binding with no `approval` block still hits a policy
                       keyed on the operator's capability/risk.
  2. required: false — strictness-only merge: `false` never disables a mandate.
  3. route-around    — N/A here (this runs at the dispatch chokepoint, not a
                       graph node the author wires); resolver is consulted
                       regardless of topology.
  4. different op    — the gate keys on declared risk + capability, so any
                       operator with that capability/risk hits the same policy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from cairn.core.engine.schema import (
    ActionCandidate,
    ActionRequest,
    PolicyDecision,
)

# cairn operator risk enum → the ActionCandidate risk enum the PolicyEngine
# filters on. Operator risk (pack.toml) is read_only|external_write|
# production_mutation|paid_compute; policy applies_to.risks is low|medium|high.
_RISK_TO_CANDIDATE: dict[str, str] = {
    "read_only": "low",
    "external_write": "medium",
    "production_mutation": "high",
    "paid_compute": "high",
}

# Policy effects that imply a human gate (deny is treated as "must not auto-run"
# → still requires a human to release; the run records the denial either way).
_GATING_EFFECTS = {"require_hitl", "require_dry_run", "deny"}


class _PolicyEvaluator(Protocol):
    """Duck-typed PolicyEngine surface (evaluate one ActionRequest)."""
    def evaluate(self, request: ActionRequest) -> PolicyDecision: ...


@dataclass(frozen=True)
class ApprovalGate:
    """The effective gate for one operation invocation.

    ``sources`` lists which authorities mandated it (operator/profile/policy/
    binding) — for the audit record + debuggability.
    """
    required: bool
    sources: tuple[str, ...] = ()
    reason: Optional[str] = None
    channel: Optional[str] = None
    approval_role: Optional[str] = None
    timeout_seconds: Optional[int] = None
    allowed_decisions: tuple[str, ...] = ("approve", "reject")
    on_expiry: str = "reject"  # reject | approve | escalate
    policy_effect: Optional[str] = None
    # what the gate keyed on — surfaced to the approver so they can decide (#55).
    risk: Optional[str] = None
    capability: Optional[str] = None


def resolve_gate(
    *,
    operator_name: str,
    risk: Optional[str],
    requires_approval: bool,
    capabilities: list[str],
    environment: str,
    policy_engine: Optional[_PolicyEvaluator] = None,
    binding_approval: Any = None,   # OperationBinding.approval (ApprovalField | None)
    profile_approval: Optional[dict] = None,  # {"mode": "off"|"enforce", "default_on_risk": [...]}
) -> ApprovalGate:
    """Resolve the effective gate by merging the four authorities.

    Merge rule (strictness-only): the gate is ON if ANY of operator /
    deployment-profile / deployment-policy / pack-binding mandates it; the
    binding can only ADD a gate or SHORTEN the timeout — never disable a
    deployment-mandated one.
    """
    sources: list[str] = []
    reason: Optional[str] = None
    channel: Optional[str] = None
    role: Optional[str] = None
    timeouts: list[int] = []
    policy_effect: Optional[str] = None

    # 1. Operator-author baseline (pack.toml requires_approval).
    if requires_approval:
        sources.append("operator")

    # 2. Deployment profile coarse switch (gate by operator-risk class).
    pa = profile_approval or {}
    if pa.get("mode") == "enforce" and risk and risk in (pa.get("default_on_risk") or []):
        sources.append("profile")

    # 3. Deployment policy (cross-cutting, keyed on capability + mapped risk).
    # Supplied by `cairn.core.approval.policy_source` at the dispatch chokepoint
    # (#1095). `None` means this deployment has no usable policy plane — the
    # source is skipped and operator-baseline + profile + binding still apply.
    # NB a policy engine that BUILDS but fails to evaluate gates below (fail-closed).
    if policy_engine is not None:
        cap = capabilities[0] if capabilities else None
        candidate = ActionCandidate(
            id=operator_name,
            type=operator_name,
            risk=_RISK_TO_CANDIDATE.get(risk or "", "medium"),  # type: ignore[arg-type]
            capability=cap,
        )
        try:
            decision = policy_engine.evaluate(
                ActionRequest(candidate=candidate, environment=environment)  # type: ignore[arg-type]
            )
        except Exception:  # noqa: BLE001 — a broken policy must fail SAFE (gate on), not open
            decision = PolicyDecision(
                effect="require_hitl",
                # sentinel: an evaluation we could not complete COUNTS as a match,
                # so the fail-closed gate below fires (unlike a clean no-match).
                matched_policies=["<policy-evaluation-failed>"],
                reasons=["policy evaluation failed"],
            )
        # #1095: gate only when a policy ACTUALLY MATCHED. The engine's no-match
        # default is `require_hitl` in production — right for the ADMISSION plane
        # (an unknown LLM-proposed remediation must ask a human), catastrophic
        # here: at the per-operation chokepoint every operator in every workflow
        # would gate. A clean no-match means the deployment said nothing about
        # this operation, so the other authorities (operator-declared risk /
        # requires_approval, profile switch, binding) decide — they already have
        # the information. An evaluation FAILURE is different and still gates
        # (see the sentinel above): unknown ≠ permitted.
        if decision.effect in _GATING_EFFECTS and decision.matched_policies:
            sources.append("policy")
            policy_effect = decision.effect
            if decision.channels:
                channel = decision.channels[0]
            role = decision.approval_role or role

    # 4. Pack binding — STRICTNESS-ONLY. May add a gate / tighten, never relax.
    if binding_approval is not None:
        if getattr(binding_approval, "required", False):
            sources.append("binding")
        if getattr(binding_approval, "reason", None):
            reason = binding_approval.reason
        if getattr(binding_approval, "channel", None):
            channel = binding_approval.channel  # narrows routing
        th = getattr(binding_approval, "timeout_hours", None)
        if th:
            timeouts.append(int(th) * 3600)

    required = bool(sources)
    timeout_seconds = min(timeouts) if timeouts else None
    return ApprovalGate(
        required=required,
        sources=tuple(dict.fromkeys(sources)),  # de-dup, preserve order
        reason=reason,
        channel=channel,
        approval_role=role,
        timeout_seconds=timeout_seconds,
        policy_effect=policy_effect,
        risk=risk,
        capability=(capabilities[0] if capabilities else None),
    )
