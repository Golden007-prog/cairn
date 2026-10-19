"""The POLICY source reaches the runtime chokepoint (#1095, ADR-0083 D3/R1).

`test_approval_resolver.py` proves `resolve_gate` honors a policy engine — but
it hands one in directly. The gap this file closes is the WIRING: until #1095,
`resolve_runtime_gate` passed `policy_engine=None` ("wired later"), so an
environment-attached policy authored in the console (#316/#317) — e.g. "gate
every external_write" — never opened a per-operation gate. Admission-time policy
(runner.py) and dispatch-time gating were two disconnected planes.

These tests drive the RUNTIME entry (`resolve_runtime_gate`), not the pure
resolver, so a future refactor that stops threading the policy engine fails here.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from cairn.core.approval import enforce
from cairn.core.engine.schema import PolicyDecision


@dataclass
class _Meta:
    """Stand-in for registry operator metadata (pack.toml-derived)."""
    risk: str = "external_write"
    requires_approval: bool = False          # the operator author did NOT ask for a gate
    provides_capabilities: tuple[str, ...] = ("storage.put",)


class _Op:
    name = "s3/put"


class _RecordingPolicy:
    """Policy engine that gates `external_write` (→ candidate risk "medium").

    Mirrors the real engine's contract: a decision reports which policies
    MATCHED, and a no-match returns the engine's safe default effect with an
    EMPTY ``matched_policies``.
    """
    def __init__(self, effect: str = "require_hitl"):
        self._effect = effect
        self.seen: list = []

    def evaluate(self, request):
        self.seen.append(request)
        if request.candidate.risk == "medium":
            return PolicyDecision(effect=self._effect, matched_policies=["gate-external-write"],
                                  reasons=["policy: gate external_write"])
        return PolicyDecision(effect="allow_auto", matched_policies=[])


@pytest.fixture
def _registry_meta(monkeypatch):
    """Point the chokepoint's metadata lookup at our fake operator."""
    from cairn.core.operator import registry as registry_mod

    monkeypatch.setattr(registry_mod.default_registry, "get_metadata",
                        lambda name: _Meta(), raising=False)
    # no profile coarse-switch: the policy must be the ONLY gating authority
    monkeypatch.setattr(enforce, "_profile_approval", lambda: None)


def test_env_policy_opens_a_gate_at_the_chokepoint(_registry_meta, monkeypatch):
    """THE regression this issue exists for: policy-only gate, nothing else set.

    Before #1095 this returned required=False — the console-authored policy was
    silently inert at dispatch."""
    policy = _RecordingPolicy()
    monkeypatch.setattr(enforce, "_runtime_policy_engine", lambda: policy)

    gate = enforce.resolve_runtime_gate(_Op(), binding=None, state={"environment": "production"})

    assert gate.required is True, "env policy must open a per-operation gate"
    assert "policy" in gate.sources
    assert gate.policy_effect == "require_hitl"
    assert policy.seen, "the policy engine must actually be consulted at dispatch"
    assert policy.seen[0].candidate.capability == "storage.put"
    assert policy.seen[0].environment == "production"


def test_allow_auto_policy_does_not_gate(_registry_meta, monkeypatch):
    """A consulted-but-permissive policy leaves the op ungated (no over-gating)."""
    monkeypatch.setattr(enforce, "_runtime_policy_engine",
                        lambda: _RecordingPolicy(effect="allow_auto"))

    gate = enforce.resolve_runtime_gate(_Op(), binding=None, state={"environment": "dev"})

    assert gate.required is False and gate.sources == ()


def test_no_matching_policy_does_not_gate_even_in_production(_registry_meta, monkeypatch):
    """The trap that made this wiring dangerous — and why it was deferred.

    `PolicyEngine.evaluate` returns `require_hitl` with EMPTY `matched_policies`
    when nothing matches in production ("safe default"). That default is right
    for the ADMISSION plane (an unknown LLM-proposed remediation must ask a
    human) and catastrophic at the per-operation chokepoint: every operator in
    every workflow would suddenly require approval. A clean no-match must leave
    gating to the operator/profile/binding authorities.
    """
    class _NoMatch:
        def evaluate(self, request):
            return PolicyDecision(effect="require_hitl", matched_policies=[],
                                  reasons=["no matching policy; safe default applied"])

    monkeypatch.setattr(enforce, "_runtime_policy_engine", lambda: _NoMatch())

    gate = enforce.resolve_runtime_gate(_Op(), binding=None, state={"environment": "production"})

    assert gate.required is False, "a no-match default must not gate every operation"
    assert gate.sources == ()


def test_no_match_still_honours_the_operator_baseline(_registry_meta, monkeypatch):
    """No-match policy is inert, but the operator's own declaration still gates."""
    from cairn.core.operator import registry as registry_mod

    monkeypatch.setattr(registry_mod.default_registry, "get_metadata",
                        lambda name: _Meta(requires_approval=True), raising=False)
    monkeypatch.setattr(
        enforce, "_runtime_policy_engine",
        lambda: type("P", (), {"evaluate": lambda self, r: PolicyDecision(
            effect="require_hitl", matched_policies=[])})(),
    )

    gate = enforce.resolve_runtime_gate(_Op(), binding=None, state={"environment": "production"})

    assert gate.required is True and gate.sources == ("operator",)


def test_deny_effect_gates(_registry_meta, monkeypatch):
    """`deny` is gating too — a denied action needs a human to release/close it."""
    monkeypatch.setattr(enforce, "_runtime_policy_engine",
                        lambda: _RecordingPolicy(effect="deny"))

    gate = enforce.resolve_runtime_gate(_Op(), binding=None, state={"environment": "production"})

    assert gate.required is True and gate.policy_effect == "deny"


def test_broken_policy_engine_fails_closed(_registry_meta, monkeypatch):
    """A policy engine that cannot be built/evaluated must gate, never open.

    Fail-CLOSED is the whole contract: an unavailable policy source is not a
    licence to auto-run an external write."""
    class _Boom:
        def evaluate(self, request):
            raise RuntimeError("policy store unreachable")

    monkeypatch.setattr(enforce, "_runtime_policy_engine", lambda: _Boom())

    gate = enforce.resolve_runtime_gate(_Op(), binding=None, state={"environment": "production"})

    assert gate.required is True
    assert "policy" in gate.sources


def test_engine_construction_failure_does_not_break_dispatch(_registry_meta, monkeypatch):
    """If the engine can't even be CONSTRUCTED, dispatch still resolves a gate.

    The other authorities (operator baseline / profile / binding) must keep
    working — a missing policy plane degrades to those, it does not crash runs.
    """
    def _explode():
        raise RuntimeError("no policy store configured")

    monkeypatch.setattr(enforce, "_runtime_policy_engine", _explode)

    gate = enforce.resolve_runtime_gate(_Op(), binding=None, state={"environment": "production"})

    # policy source unavailable → no gate from policy, but no exception either
    assert gate.required is False


def test_shared_engine_is_reused(monkeypatch):
    """P2: one evaluator instance, not a fresh PolicyEngine per operation.

    Constructing one per dispatch re-reads every pack policy dir + the store on
    every operator call — and lets two call sites disagree."""
    from cairn.core.approval import policy_source

    policy_source.reset_runtime_policy_engine()
    built: list[int] = []

    class _Fake:
        def __init__(self):
            built.append(1)

        def evaluate(self, request):
            return PolicyDecision(effect="allow_auto")

    monkeypatch.setattr(policy_source, "_build_engine", lambda: _Fake())

    a = policy_source.runtime_policy_engine()
    b = policy_source.runtime_policy_engine()

    assert a is b, "the runtime policy engine must be a shared singleton"
    assert len(built) == 1, "the engine must be constructed once, not per call"
    policy_source.reset_runtime_policy_engine()
