"""§24.3.4 P2.f — capability-scoped policy attachment.

Pins:

  - ``PolicyAppliesTo.capabilities`` parses + round-trips.
  - ``ActionCandidate.capability`` parses + round-trips (optional).
  - ``policy_applies`` honors the capability filter with AND semantics
    relative to the existing dimensions (action_types / environments /
    risks).
  - The compliance-pack-style policy works end-to-end through the
    PolicyEngine: a policy targeting only ``capabilities`` (no
    action_types) fires on any candidate that names the capability.
  - Backward compat: today's policies (no capabilities filter) keep
    working, and actions without a declared capability keep matching
    legacy policies.
"""
from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.engine.schema import (
    ActionCandidate,
    ActionRequest,
    PolicyAppliesTo,
    PolicyRule,
    PolicySpec,
)
from cairn.core.policy.engine import (
    PolicyEngine,
    load_policy,
    policy_applies,
)


# ─── helpers ───────────────────────────────────────────────────────


def _request(
    *,
    type_: str = "post_message",
    capability: str | None = None,
    environment: str = "production",
    risk: str = "low",
) -> ActionRequest:
    return ActionRequest(
        candidate=ActionCandidate(
            id="a-1", type=type_, risk=risk, capability=capability,
        ),
        environment=environment,
    )


def _policy(
    *,
    capabilities: list[str] | None = None,
    action_types: list[str] | None = None,
    environments: list[str] | None = None,
    risks: list[str] | None = None,
    effect: str = "require_hitl",
    name: str = "test-policy",
) -> PolicySpec:
    return PolicySpec(
        name=name,
        applies_to=PolicyAppliesTo(
            action_types=action_types or [],
            environments=environments or [],
            risks=risks or [],
            capabilities=capabilities or [],
        ),
        rules=[PolicyRule(effect=effect)],
    )


# ─── schema round-trip ────────────────────────────────────────────


def test_policy_applies_to_capabilities_defaults_to_empty():
    """Pre-P2.f policies (no capabilities field) round-trip with []."""
    spec = PolicyAppliesTo(action_types=["k8s_scale"])
    assert spec.capabilities == []


def test_policy_applies_to_capabilities_round_trips():
    spec = PolicyAppliesTo(capabilities=["comms.post_message", "ticket.create"])
    assert spec.capabilities == ["comms.post_message", "ticket.create"]
    # And via the serialized form, since policies arrive from YAML.
    rebuilt = PolicyAppliesTo.model_validate(spec.model_dump())
    assert rebuilt.capabilities == ["comms.post_message", "ticket.create"]


def test_action_candidate_capability_defaults_to_none():
    c = ActionCandidate(id="a", type="post_message")
    assert c.capability is None


def test_action_candidate_capability_round_trips():
    c = ActionCandidate(id="a", type="post_message",
                        capability="comms.post_message")
    assert c.capability == "comms.post_message"
    rebuilt = ActionCandidate.model_validate(c.model_dump())
    assert rebuilt.capability == "comms.post_message"


# ─── policy_applies matcher ───────────────────────────────────────


def test_no_capability_filter_keeps_legacy_behavior():
    """Policies that don't declare capabilities apply by the
    pre-P2.f rules — no regression for today's deployments."""
    pol = _policy(action_types=["post_message"])
    req = _request(type_="post_message", capability=None)
    assert policy_applies(pol, req)


def test_capability_filter_matches_candidate_capability():
    pol = _policy(capabilities=["comms.post_message"])
    req = _request(capability="comms.post_message")
    assert policy_applies(pol, req)


def test_capability_filter_rejects_non_matching_capability():
    pol = _policy(capabilities=["comms.post_message"])
    req = _request(capability="ticket.create")
    assert not policy_applies(pol, req)


def test_capability_filter_rejects_action_without_declared_capability():
    """A policy targeting a capability cannot match an action that
    doesn't tell us which capability it invokes — the safe answer is
    'no, this policy doesn't apply', not 'yes, the filter is empty'."""
    pol = _policy(capabilities=["comms.post_message"])
    req = _request(capability=None)
    assert not policy_applies(pol, req)


def test_capability_filter_ands_with_environment_filter():
    """AND semantics: both filters must match. Lets a policy target
    'comms.post_message in production only', which is the actual
    compliance-pack pattern."""
    pol = _policy(
        capabilities=["comms.post_message"],
        environments=["production"],
    )
    prod_req = _request(capability="comms.post_message", environment="production")
    dev_req = _request(capability="comms.post_message", environment="dev")
    assert policy_applies(pol, prod_req)
    assert not policy_applies(pol, dev_req)


def test_capability_filter_ands_with_action_type_filter():
    """A policy can scope by BOTH 'these capabilities' AND 'these
    action types' — the dimensions don't override each other."""
    pol = _policy(
        capabilities=["comms.post_message"],
        action_types=["post_message"],
    )
    matching = _request(type_="post_message", capability="comms.post_message")
    wrong_type = _request(type_="ticket_create", capability="comms.post_message")
    assert policy_applies(pol, matching)
    assert not policy_applies(pol, wrong_type)


# ─── end-to-end through PolicyEngine ──────────────────────────────


def test_capability_targeted_policy_fires_via_engine(tmp_path):
    """Compliance-pack pattern end-to-end: a policy with ONLY
    `capabilities` (no action_types) loaded from YAML fires on any
    action that invokes the named capability."""
    p = tmp_path / "redact.yaml"
    p.write_text(dedent("""
        name: compliance.redact-pii-before-comms
        description: Require dry-run before any external comms post.
        applies_to:
          capabilities:
            - comms.post_message
        rules:
          - effect: require_dry_run
            reason: PII scan required
    """))
    engine = PolicyEngine(tmp_path)
    req = _request(
        type_="post_message",
        capability="comms.post_message",
        environment="staging",
    )
    decision = engine.evaluate(req)
    assert decision.effect == "require_dry_run"
    assert "compliance.redact-pii-before-comms" in decision.matched_policies
    assert any("PII scan required" in r for r in decision.reasons)


def test_capability_targeted_policy_does_not_fire_on_other_capability(tmp_path):
    """The same compliance policy should leave unrelated actions alone."""
    p = tmp_path / "redact.yaml"
    p.write_text(dedent("""
        name: compliance.redact-pii-before-comms
        applies_to:
          capabilities:
            - comms.post_message
        rules:
          - effect: require_dry_run
    """))
    engine = PolicyEngine(tmp_path)
    req = _request(
        type_="k8s_scale",
        capability="k8s.scale_deployment",
        environment="staging",
    )
    decision = engine.evaluate(req)
    # Default safe behavior — no policy matched.
    assert decision.matched_policies == []


def test_yaml_loads_policy_with_capabilities_field(tmp_path):
    """A YAML policy carrying the new field round-trips through
    load_policy() — pinning the schema surface for pack authors."""
    p = tmp_path / "cap-policy.yaml"
    p.write_text(dedent("""
        name: cap-only
        applies_to:
          capabilities:
            - x.y
            - x.z
        rules:
          - effect: require_hitl
    """))
    spec = load_policy(p)
    assert spec.applies_to.capabilities == ["x.y", "x.z"]
