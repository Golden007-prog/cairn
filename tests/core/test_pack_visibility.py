"""§24.3 P2.d — encapsulation at the lookup seam.

The §24.3 truth table over (from_pack, owning_pack, encapsulated, in
provides) is tested directly — one test per row, parametrized where
the row collapses naturally.

These are pure-logic tests on synthetic packs — no registry, no
entry-points. The integration with the live registry is exercised by
the global-default test at the bottom and indirectly by everything in
test_pack_composition.py (which clears + injects packs).
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.pack import (
    CapabilityBinding,
    Pack,
    PackManifest,
    clear,
    is_capability_visible,
    owning_pack_for_capability,
    register_pack,
    visible_capability_bindings,
)


# ─── helpers ───────────────────────────────────────────────────────


def _binding(cap: str) -> CapabilityBinding:
    async def _noop(**_kw):  # pragma: no cover
        return None
    return CapabilityBinding(
        capability=cap, func=_noop,
        description=cap, name_for_llm=cap.replace(".", "_"),
    )


def _pack(name, *, provides_caps=None, bindings=None, encapsulated=True):
    manifest = PackManifest(
        name=name, version="0.0.1",
        provides_declared=encapsulated,
        provides_capabilities=list(provides_caps or []),
    )
    return Pack(
        name=name, version="0.0.1", description="",
        capability_bindings=[_binding(c) for c in (bindings or [])],
        manifest=manifest,
    )


# ─── truth table: is_capability_visible ───────────────────────────


def test_within_pack_always_visible_even_when_private():
    """A pack sees its own private capabilities — encapsulation is
    outward-facing, not internal."""
    p = _pack("incident", provides_caps=["incident.investigate"],
              bindings=["incident.investigate", "incident.helper"])
    # 'incident.helper' is private (not in provides_caps) — but a caller
    # *inside* incident still sees it.
    assert is_capability_visible(
        "incident.helper", owning_pack=p, from_pack="incident",
    )


def test_permissive_owner_visible_to_anyone():
    """Pack with no [provides] uses today's everything-visible default
    (§24.3.10 migration on-ramp)."""
    legacy = _pack("legacy", bindings=["logs.read"], encapsulated=False)
    assert is_capability_visible("logs.read", owning_pack=legacy,
                                 from_pack="other")


def test_encapsulated_exported_visible_cross_pack():
    """The happy-path encapsulation: encapsulated pack X explicitly
    exports `cap` → caller Y sees it."""
    p = _pack("comms", provides_caps=["comms.post_message"],
              bindings=["comms.post_message"])
    assert is_capability_visible(
        "comms.post_message", owning_pack=p, from_pack="incident",
    )


def test_encapsulated_private_invisible_cross_pack():
    """The whole point of encapsulation: a binding that exists but
    isn't in `provides_capabilities` is invisible to outsiders."""
    p = _pack("comms", provides_caps=["comms.post_message"],
              bindings=["comms.post_message", "comms._internal_retry"])
    assert not is_capability_visible(
        "comms._internal_retry", owning_pack=p, from_pack="incident",
    )


def test_global_caller_preserves_legacy_visibility():
    """`from_pack=None` represents the engine / CLI / MCP gateway —
    callers that don't (yet) carry a calling-pack identity. They see
    the full surface to preserve today's behavior; selective
    encapsulation only applies once a caller opts in by passing its name."""
    p = _pack("comms", provides_caps=["comms.post_message"],
              bindings=["comms.post_message", "comms._private"])
    assert is_capability_visible(
        "comms._private", owning_pack=p, from_pack=None,
    )


# ─── visible_capability_bindings filtered walk ─────────────────────


def test_filtered_walk_drops_cross_pack_private():
    """The aggregator drops private bindings when a cross-pack caller
    is identified — the foundational seam for §24.3 enforcement."""
    comms = _pack("comms", provides_caps=["comms.post_message"],
                  bindings=["comms.post_message", "comms._internal"])
    incident = _pack("incident", provides_caps=["incident.investigate"],
                     bindings=["incident.investigate"])
    out = visible_capability_bindings(from_pack="incident",
                                      packs=[comms, incident])
    caps = {b.capability for b in out}
    # incident sees its own everything + comms's public surface only.
    assert caps == {"comms.post_message", "incident.investigate"}


def test_filtered_walk_within_pack_sees_everything():
    """A pack walking the registry as 'itself' sees its own private
    bindings too (within-pack rule)."""
    comms = _pack("comms", provides_caps=["comms.post_message"],
                  bindings=["comms.post_message", "comms._internal"])
    out = visible_capability_bindings(from_pack="comms", packs=[comms])
    caps = {b.capability for b in out}
    assert caps == {"comms.post_message", "comms._internal"}


def test_filtered_walk_no_caller_equals_legacy_view():
    """`from_pack=None` returns the same flat list as today's
    `all_capability_bindings()` — a smoke contract for backward compat."""
    comms = _pack("comms", provides_caps=["comms.post_message"],
                  bindings=["comms.post_message", "comms._internal"])
    out_filtered = visible_capability_bindings(from_pack=None, packs=[comms])
    assert {b.capability for b in out_filtered} == {
        "comms.post_message", "comms._internal",
    }


def test_filtered_walk_with_permissive_pack_drops_nothing():
    """Permissive pack contributions are visible to every caller —
    matches §24.3.10."""
    legacy = _pack("legacy", bindings=["logs.read", "metrics.query"],
                   encapsulated=False)
    out = visible_capability_bindings(from_pack="incident", packs=[legacy])
    assert {b.capability for b in out} == {"logs.read", "metrics.query"}


# ─── owning_pack_for_capability ────────────────────────────────────


def test_owning_pack_lookup_finds_provider():
    comms = _pack("comms", bindings=["comms.post_message"])
    incident = _pack("incident", bindings=["incident.investigate"])
    assert (owning_pack_for_capability(
        "comms.post_message", packs=[comms, incident]) == "comms")
    assert (owning_pack_for_capability(
        "incident.investigate", packs=[comms, incident]) == "incident")


def test_owning_pack_lookup_returns_none_when_missing():
    comms = _pack("comms", bindings=["comms.post_message"])
    assert (owning_pack_for_capability(
        "nonexistent", packs=[comms]) is None)


# ─── registry integration ─────────────────────────────────────────


def test_uses_live_registry_when_packs_kwarg_omitted(monkeypatch):
    """The aggregator defaults to the live pack registry — proves the
    P2.d primitives are wired into the real consumption path, not just
    test-injectable in isolation."""
    monkeypatch.delenv("OBS_PROFILE", raising=False)
    clear()
    try:
        comms = _pack("comms-test", provides_caps=["comms.post_message"],
                      bindings=["comms.post_message", "comms._internal"])
        register_pack(comms)
        # Cross-pack call (no caller named comms-test) sees only the
        # public surface.
        out = visible_capability_bindings(from_pack="other")
        caps = {b.capability for b in out}
        assert "comms.post_message" in caps
        assert "comms._internal" not in caps
    finally:
        clear()
