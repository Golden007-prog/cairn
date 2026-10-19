"""Tests for the ApprovalResolver — the non-bypassable HITL gate decision (ADR-0034).

Covers the strictness-only merge + all four bypass vectors. The PolicyEngine is
a duck-typed stub so these stay pure (no policy files, no graph).
"""
from __future__ import annotations

from cairn.core.approval.resolver import resolve_gate
from cairn.core.engine.schema import PolicyDecision
from cairn.core.operator.binding import ApprovalField


class _Policy:
    """Stub PolicyEngine: returns `effect` only when the request matches the
    optional capability/risk filter (else allow_auto), or raises if `boom`.

    Mirrors the real engine's contract on ``matched_policies`` (#1095): a hit
    names the policies that matched; a miss returns an EMPTY list. The resolver
    gates on the policy source only when something actually matched — the
    engine's no-match default is ``require_hitl`` in production, which must not
    gate every operation at the dispatch chokepoint.
    """
    def __init__(self, effect="allow_auto", *, channels=None, role=None,
                 only_cap=None, only_risk=None, boom=False):
        self._e, self._ch, self._role = effect, channels or [], role
        self._only_cap, self._only_risk, self._boom = only_cap, only_risk, boom

    def evaluate(self, request):
        if self._boom:
            raise RuntimeError("policy blew up")
        if self._only_cap and request.candidate.capability != self._only_cap:
            return PolicyDecision(effect="allow_auto", matched_policies=[])
        if self._only_risk and request.candidate.risk != self._only_risk:
            return PolicyDecision(effect="allow_auto", matched_policies=[])
        return PolicyDecision(effect=self._e, matched_policies=["stub-policy"],
                              channels=self._ch, approval_role=self._role)


def _resolve(**kw):
    base = dict(
        operator_name="k8s/scale",
        risk="external_write",
        requires_approval=False,
        capabilities=["infra.scale"],
        environment="production",
        policy_engine=_Policy(),
        binding_approval=None,
        profile_approval=None,
    )
    base.update(kw)
    return resolve_gate(**base)


def test_no_authority_no_gate():
    g = _resolve()
    assert g.required is False and g.sources == ()


def test_operator_baseline_gates():
    g = _resolve(requires_approval=True)
    assert g.required and "operator" in g.sources


# ── Bypass vector 1: omission (no binding) — a capability policy still fires ──
def test_policy_on_capability_gates_without_binding():
    g = _resolve(policy_engine=_Policy("require_hitl", channels=["slack:#oncall"],
                                       role="sre", only_cap="infra.scale"))
    assert g.required and "policy" in g.sources
    assert g.channel == "slack:#oncall" and g.approval_role == "sre"


# ── Bypass vector 2: required:false cannot disable a deployment mandate ──────
def test_binding_false_cannot_relax_policy():
    g = _resolve(
        policy_engine=_Policy("require_hitl", only_cap="infra.scale"),
        binding_approval=ApprovalField(required=False),
    )
    assert g.required and "policy" in g.sources  # binding false is a no-op vs the mandate


def test_binding_false_cannot_relax_operator_baseline():
    g = _resolve(requires_approval=True, binding_approval=ApprovalField(required=False))
    assert g.required and "operator" in g.sources


# ── Bypass vector 4: different operator — gate keys on risk (mapped) ─────────
def test_policy_on_mapped_risk_gates():
    # production_mutation maps to candidate risk "high"; a policy gating "high" fires.
    g = _resolve(risk="production_mutation",
                 policy_engine=_Policy("require_hitl", only_risk="high"))
    assert g.required and "policy" in g.sources
    # a read_only op (maps to "low") with the same policy is NOT gated
    g2 = _resolve(risk="read_only", policy_engine=_Policy("require_hitl", only_risk="high"))
    assert g2.required is False


# ── binding can ADD / tighten (strictness-only, the allowed direction) ───────
def test_binding_can_add_gate():
    g = _resolve(binding_approval=ApprovalField(required=True, reason="prod scale",
                                                timeout_hours=1))
    assert g.required and "binding" in g.sources
    assert g.reason == "prod scale" and g.timeout_seconds == 3600


def test_profile_enforce_gates_on_risk():
    g = _resolve(risk="production_mutation",
                 profile_approval={"mode": "enforce", "default_on_risk": ["production_mutation"]})
    assert g.required and "profile" in g.sources


def test_profile_off_does_not_gate():
    g = _resolve(risk="production_mutation",
                 profile_approval={"mode": "off", "default_on_risk": ["production_mutation"]})
    assert g.required is False


def test_policy_failure_fails_safe():
    g = _resolve(policy_engine=_Policy(boom=True))
    assert g.required and g.policy_effect == "require_hitl"


def test_multiple_sources_recorded():
    g = _resolve(requires_approval=True,
                 policy_engine=_Policy("require_hitl", only_cap="infra.scale"),
                 binding_approval=ApprovalField(required=True))
    assert set(g.sources) == {"operator", "policy", "binding"}
