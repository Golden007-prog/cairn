"""§24.3 P2.c — doctor's ``[packs]`` category renders the composition report.

Pure CLI-layer tests: synthesises a registry state (clear → register
fake packs → call check_composition) and asserts the rendered rows
carry the right status + category. The underlying validate_composition
machinery is exercised in test_pack_composition.py — these tests pin
the *presentation* contract.

The format_report layout is covered too, because doctor's exit code
depends on category ordering for noisy categories not to mask the
[packs] section.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.cli.commands import doctor
from cairn.core.pack import (
    CapabilityBinding,
    Pack,
    PackManifest,
    clear,
    register_pack,
    set_composition_report,
)
from cairn.core.pack.composition import (
    CompositionReport,
    validate_composition,
)


# ─── helpers (mirror tests/core/test_pack_composition.py) ──────────


def _binding(cap: str) -> CapabilityBinding:
    async def _noop(**_kw):  # pragma: no cover
        return None
    return CapabilityBinding(
        capability=cap, func=_noop,
        description=cap, name_for_llm=cap.replace(".", "_"),
    )


def _pack(name, *, provides_caps=None, requires_caps=None,
          optional_caps=None, bindings=None, encapsulated=True):
    manifest = PackManifest(
        name=name, version="0.0.1",
        provides_declared=encapsulated,
        provides_capabilities=list(provides_caps or []),
        required_capabilities=list(requires_caps or []),
        optional_capabilities=list(optional_caps or []),
    )
    return Pack(
        name=name, version="0.0.1", description="",
        capability_bindings=[_binding(c) for c in (bindings or [])],
        manifest=manifest,
    )


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """Every test in this module runs with a clean pack registry — we
    inject synthetic packs and don't want the real incident pack to
    bleed in via ensure_loaded()."""
    monkeypatch.delenv("OBS_PROFILE", raising=False)
    clear()
    yield
    clear()


# ─── happy path ────────────────────────────────────────────────────


def test_no_packs_installed_warns(monkeypatch):
    """A deployment with zero packs is unusable but not a hard fail —
    doctor surfaces the diagnostic so the operator sees what's missing.

    The autouse fixture clears the registry, but ensure_loaded() walks
    entry-points and re-discovers the first-party incident pack. Stub
    it here so we can simulate "no packs installed."
    """
    monkeypatch.setattr(
        "cairn.core.pack.ensure_loaded",
        lambda *a, **kw: [],
    )
    results = doctor.check_composition()
    assert len(results) == 1
    r = results[0]
    assert r.name == "packs_loaded"
    assert r.status == "warn"
    assert r.category == "packs"
    assert "no packs installed" in r.detail


def test_single_pack_clean_emits_two_ok_rows():
    """One pack, no conflicts, no warnings → the two summary rows are
    present and both ok. Operator rows from Phase 2.2 may add ok/warn
    rows for any entry-point-discovered operators in the registry; this
    test only pins the composition summary rows."""
    register_pack(_pack(
        "incident",
        provides_caps=["incident.investigate", "incident.generate_rca"],
    ))
    set_composition_report(validate_composition([_pack(
        "incident",
        provides_caps=["incident.investigate", "incident.generate_rca"],
    )]))
    results = doctor.check_composition()
    statuses = {r.name: r.status for r in results}
    assert statuses["packs_loaded"] == "ok"
    assert statuses["capabilities_provided"] == "ok"
    # No composition fails — the summary rows themselves are clean.
    assert not any(
        r.status == "fail" and r.name.startswith("composition_")
        for r in results
    )
    # Detail names the pack.
    loaded = next(r for r in results if r.name == "packs_loaded")
    assert "incident" in loaded.detail
    # Capabilities count surfaces.
    caps = next(r for r in results if r.name == "capabilities_provided")
    assert "2 capability" in caps.detail


# ─── conflicts → fail rows ─────────────────────────────────────────


def test_duplicate_provider_renders_as_fail_row():
    slack = _pack("slack-comms", provides_caps=["comms.post_message"])
    discord = _pack("discord-comms", provides_caps=["comms.post_message"])
    register_pack(slack)
    register_pack(discord)
    set_composition_report(validate_composition([slack, discord]))

    results = doctor.check_composition()
    fails = [r for r in results if r.status == "fail"]
    assert len(fails) == 1
    assert fails[0].category == "packs"
    assert fails[0].name == "composition_conflict"
    assert "comms.post_message" in fails[0].detail
    assert "slack-comms" in fails[0].detail
    assert "discord-comms" in fails[0].detail


def test_unresolved_required_capability_renders_as_fail():
    incident = _pack("incident", provides_caps=["incident.investigate"],
                     requires_caps=["logs.read"])
    register_pack(incident)
    set_composition_report(validate_composition([incident]))

    results = doctor.check_composition()
    fails = [r for r in results if r.status == "fail"]
    assert any("logs.read" in r.detail for r in fails)


def test_multiple_conflicts_all_rendered():
    """Operator wants one row per problem, not a coalesced summary."""
    a = _pack("a", provides_caps=["x"], requires_caps=["missing.one", "missing.two"])
    b = _pack("b", provides_caps=["x"])
    register_pack(a)
    register_pack(b)
    set_composition_report(validate_composition([a, b]))

    fails = [r for r in doctor.check_composition() if r.status == "fail"]
    assert len(fails) >= 3  # 2 missing + 1 duplicate


# ─── warnings → warn rows ──────────────────────────────────────────


def test_unmet_optional_renders_as_warn_row():
    incident = _pack("incident", provides_caps=["incident.investigate"],
                     optional_caps=["compliance.check"])
    register_pack(incident)
    set_composition_report(validate_composition([incident]))

    results = doctor.check_composition()
    warns = [r for r in results if r.status == "warn"]
    # Filter to composition warnings — Phase 2.2 added operator-pack rows
    # whose warn status depends on registry state independent of this
    # test's fixture.
    comp_warns = [r for r in warns if r.name == "composition_warning"]
    assert len(comp_warns) == 1
    assert "compliance.check" in comp_warns[0].detail
    assert comp_warns[0].category == "packs"
    # No fails — optionals don't blow the exit code.
    assert not any(r.status == "fail" for r in results)


# ─── profile-resolved conflicts don't leak through as fail rows ────


def test_profile_resolved_conflict_emits_no_fail():
    """When the profile resolves the duplicate, doctor reports cleanly —
    no fail row, exit code stays 0. The operator's choice is honored
    silently; if they wanted to *see* the underlying competition they'd
    look at [profile.capabilities] in their config."""
    slack = _pack("slack-comms", provides_caps=["comms.post_message"])
    discord = _pack("discord-comms", provides_caps=["comms.post_message"])
    register_pack(slack)
    register_pack(discord)
    set_composition_report(validate_composition(
        [slack, discord],
        profile_overrides={"comms.post_message": "slack-comms"},
    ))

    results = doctor.check_composition()
    assert not any(r.status == "fail" for r in results)
    # The cap summary counts the post-override single provider.
    caps = next(r for r in results if r.name == "capabilities_provided")
    assert "1 capability" in caps.detail


# ─── report missing → defensive warn ───────────────────────────────


def test_missing_report_still_renders_with_warn(monkeypatch):
    """If somehow the loader runs but doesn't cache a report (shouldn't
    happen — defensive only), doctor falls back to a warn row instead
    of crashing."""
    register_pack(_pack("incident", provides_caps=["incident.investigate"]))
    # Force the report cache to None *after* register; ensure_loaded()
    # in doctor would normally re-validate, so we monkeypatch it out.
    from cairn.cli.commands import doctor as doctor_mod
    monkeypatch.setattr(
        "cairn.core.pack.ensure_loaded",
        lambda *a, **kw: [],
    )
    monkeypatch.setattr(
        "cairn.core.pack.get_composition_report",
        lambda: None,
    )
    monkeypatch.setattr(
        "cairn.core.pack.get_packs",
        lambda: [_pack("incident", provides_caps=["incident.investigate"])],
    )

    results = doctor_mod.check_composition()
    assert any(r.name == "composition" and r.status == "warn"
               for r in results)


# ─── format_report includes the [packs] section ────────────────────


def test_format_report_groups_packs_section_under_packs_header():
    """The [packs] header must appear in the rendered output and group
    the composition rows under it — that's the operator-facing contract."""
    results = [
        doctor.CheckResult("packs_loaded", "ok", "1: incident", category="packs"),
        doctor.CheckResult("capabilities_provided", "ok",
                           "2 capability(ies)", category="packs"),
        doctor.CheckResult("composition_conflict", "fail",
                           "capability 'x' provided by multiple packs",
                           category="packs"),
    ]
    out = doctor.format_report(results)
    assert "[packs]" in out
    assert "1: incident" in out
    # Conflict row rendered with the fail symbol (✗).
    assert "✗ composition_conflict" in out


def test_exit_code_fails_on_composition_fail():
    results = [
        doctor.CheckResult("composition_conflict", "fail",
                           "capability 'x' provided by multiple packs",
                           category="packs"),
    ]
    assert doctor.exit_code(results) == 1


def test_exit_code_zero_on_composition_warn_only():
    """An unmet optional doesn't fail doctor by default — operator
    opts into --strict if they want zero-tolerance."""
    results = [
        doctor.CheckResult("composition_warning", "warn",
                           "pack 'incident' optional capability 'x' unprovided",
                           category="packs"),
    ]
    assert doctor.exit_code(results) == 0
