"""Named launch environments must be JUDGED, not crash the policy plane (#1766).

`launch_pipeline` has always accepted a free-form `environment` string, but
`ActionRequest.environment` was `Literal["dev","staging","production"]`. So a
pipeline launched into `campaign` raised a pydantic ValidationError *inside*
`resolve_gate`, whose except-branch treats an evaluation it could not complete
as a match (the fail-closed sentinel) — turning every task leg into
`require_hitl` and, with #1759, killing the pipeline outright.

Two real runs died this way on 2026-08-28 (pl_4d1b527c22cb47e0,
pl_0eb820efc9844574) before the cause was found; the workaround was to launch
with NO environment, which silently dropped it from the record.

The fix must hold three lines at once:
  1. a named environment evaluates instead of raising;
  2. it is judged at the STRICTEST tier when unrecognized (never a relaxation);
  3. a policy scoped to `production` still governs a named production-tier env.
"""
from __future__ import annotations

import pytest

from cairn.core.engine.schema import (
    ActionCandidate,
    ActionRequest,
    PolicySpec,
    policy_tier,
)
from cairn.core.policy.engine import PolicyEngine, policy_applies


def _req(environment: str, *, risk: str = "high", type_: str = "dataset/replay") -> ActionRequest:
    return ActionRequest(
        candidate=ActionCandidate(id=type_, type=type_, risk=risk),
        environment=environment,
    )


def _spec(name: str, *, effect: str = "require_hitl", environments=None, risks=None) -> PolicySpec:
    return PolicySpec.model_validate({
        "name": name,
        "description": name,
        "applies_to": {"environments": environments or [], "risks": risks or []},
        "rules": [{"effect": effect, "reason": name}],
    })


# ── 1. a named environment must not raise ────────────────────────────────────
def test_named_environment_is_accepted():
    """The regression itself: constructing the request used to raise."""
    r = _req("campaign")
    assert r.environment == "campaign"
    assert r.tier == "production"


@pytest.mark.parametrize("name", ["dev", "staging", "production"])
def test_tier_names_pass_through(name):
    assert policy_tier(name) == name
    assert _req(name).tier == name


@pytest.mark.parametrize("name", ["campaign", "staging-eu", "pilot-3", "", "PROD"])
def test_unknown_names_resolve_to_the_strictest_tier(name):
    """Fail-safe direction: an unrecognized environment is judged as production,
    never as dev. `PROD` is here on purpose — case-folding must not silently
    make an uppercase name a NEW tier."""
    assert policy_tier(name) == "production"


# ── 2. matching: by name OR by tier ──────────────────────────────────────────
def test_production_scoped_policy_governs_a_named_production_tier_env():
    spec = _spec("prod-only", environments=["production"])
    assert policy_applies(spec, _req("campaign")) is True


def test_a_policy_may_name_an_environment_exactly():
    spec = _spec("campaign-only", environments=["campaign"])
    assert policy_applies(spec, _req("campaign")) is True
    assert policy_applies(spec, _req("production")) is False


def test_dev_scoped_policy_does_not_leak_onto_a_named_env():
    """Widening must ADD coverage, not move it: a dev-scoped policy must not
    start governing a production-tier environment."""
    spec = _spec("dev-only", environments=["dev"])
    assert policy_applies(spec, _req("campaign")) is False
    assert policy_applies(spec, _req("dev")) is True


# ── 3. the no-match default is keyed on the tier ─────────────────────────────
def test_no_match_default_is_strict_for_a_named_env(tmp_path):
    """With no policy matching, a named environment must inherit production's
    default (require_hitl), not dev's allow_auto."""
    eng = PolicyEngine(policies_directory=tmp_path)  # empty dir = no policies
    assert eng.evaluate(_req("campaign")).effect == "require_hitl"
    assert eng.evaluate(_req("dev")).effect == "allow_auto"


# ── 4. the end-to-end shape the run actually hit ─────────────────────────────
def test_named_env_with_no_matching_policy_does_not_gate_a_task_leg(tmp_path):
    """`resolve_gate` only gates when a policy ACTUALLY matched (#1095). The bug
    was that a crash counted as a match; with the schema fixed, a clean no-match
    in a named environment leaves the other authorities to decide — which is
    what lets a governed pipeline run at all."""
    from cairn.core.approval.resolver import resolve_gate

    eng = PolicyEngine(policies_directory=tmp_path)
    gate = resolve_gate(
        operator_name="dataset/replay",
        risk="external_write",
        requires_approval=False,
        capabilities=[],
        environment="campaign",
        policy_engine=eng,
        binding_approval=None,
        profile_approval=None,
    )
    assert gate.required is False, f"named env still gates: sources={gate.sources}"


def test_a_matching_policy_still_gates_in_a_named_env(tmp_path):
    """The other direction — the fix must not disarm policies that DO apply."""
    from cairn.core.approval.resolver import resolve_gate

    # `external_write` maps to candidate risk "medium" (_RISK_TO_CANDIDATE) —
    # not "high". Scoping the policy to the mapped value keeps this test about
    # the ENVIRONMENT match, which is what #1766 is.
    (tmp_path / "p.yaml").write_text(
        "name: medium-risk-everywhere\n"
        "description: all medium-risk actions need a human\n"
        "applies_to:\n"
        "  risks: [medium]\n"
        "rules:\n"
        "  - effect: require_hitl\n"
        "    reason: medium-risk action\n",
        encoding="utf-8",
    )
    eng = PolicyEngine(policies_directory=tmp_path)
    gate = resolve_gate(
        operator_name="dataset/replay",
        risk="external_write",   # → candidate risk "medium"
        requires_approval=False,
        capabilities=[],
        environment="campaign",
        policy_engine=eng,
        binding_approval=None,
        profile_approval=None,
    )
    assert gate.required is True
    assert "policy" in gate.sources
