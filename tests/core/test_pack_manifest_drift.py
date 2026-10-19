"""Manifest ↔ code drift is LOUD, not a log line (#1105 option B, ADR-0083 D4).

Every operator is declared twice — once in code (`@register_operator` + the
Operation class) and once in `pack.toml`. The two drift, and because the manifest
is what governance reads (risk → gates), a drifted manifest is a
governance-relevant inconsistency, not cosmetics.

This bit twice in one session:

* `rag/parse` — the manifest declares it, no class is registered. The loader logs
  a warning on EVERY run that nobody acts on.
* #1100 — the entry point existed but the installed distribution's metadata was
  stale until the pack was reinstalled, so the operator was invisible.

`cairn pack check` turns both into an explicit, actionable failure.

Scope note (#1105): full manifest GENERATION is not possible yet —
`register_operator` captures only name/version/capabilities, while risk,
requires_approval, supports_* and executors live solely in `pack.toml`. Moving
those into the decorator means touching 127 call sites and is tracked separately.
This checks what IS derivable, which is where the demonstrated pain is.
"""
from __future__ import annotations

import pytest

from cairn.core.pack.drift import DriftFinding, check_operator_drift


class _Meta:
    """Stands in for a manifest OperatorEntry."""

    def __init__(self, version="0.1.0", provides=(), input_schema=None, output_schema=None):
        self.version = version
        self.provides_capabilities = list(provides)
        self.input_schema = input_schema
        self.output_schema = output_schema


class _Cls:
    """Stands in for a registered Operation subclass."""

    def __init__(self, name, version="0.1.0", capabilities=(), inp=None, out=None):
        self.name = name
        self.version = version
        self.capabilities = list(capabilities)
        self.input_schema = type(inp, (), {}) if inp else None
        self.output_schema = type(out, (), {}) if out else None


def _find(findings: list[DriftFinding], kind: str) -> list[DriftFinding]:
    return [f for f in findings if f.kind == kind]


# ── the two drifts that actually happened ────────────────────────────────────

def test_manifest_declares_an_operator_with_no_class():
    """The `rag/parse` case — a warning today, a failure now."""
    findings = check_operator_drift(
        declared={"rag/parse": _Meta()},
        registered={},
    )
    orphans = _find(findings, "declared_not_registered")
    assert orphans and "rag/parse" in orphans[0].detail
    assert "entry-point" in orphans[0].detail or "entry point" in orphans[0].detail


def test_registered_class_missing_from_the_manifest():
    """The reinstall/stale-metadata case, from the other direction. Governance
    reads the manifest, so an operator with no entry has NO declared risk."""
    findings = check_operator_drift(
        declared={},
        registered={"dataset/redact": _Cls("dataset/redact")},
    )
    missing = _find(findings, "registered_not_declared")
    assert missing and "dataset/redact" in missing[0].detail
    assert "risk" in missing[0].detail.lower(), "must say WHY it matters"


# ── field-level drift ────────────────────────────────────────────────────────

def test_version_mismatch_is_reported():
    findings = check_operator_drift(
        declared={"a/b": _Meta(version="0.2.0")},
        registered={"a/b": _Cls("a/b", version="0.1.0")},
    )
    v = _find(findings, "version_mismatch")
    assert v and "0.2.0" in v[0].detail and "0.1.0" in v[0].detail


def test_capability_mismatch_is_reported():
    findings = check_operator_drift(
        declared={"a/b": _Meta(provides=["storage.put"])},
        registered={"a/b": _Cls("a/b", capabilities=["storage.get"])},
    )
    c = _find(findings, "capability_mismatch")
    assert c and "storage.put" in c[0].detail and "storage.get" in c[0].detail


def test_schema_mismatch_is_reported():
    findings = check_operator_drift(
        declared={"a/b": _Meta(input_schema="PutInput", output_schema="PutOutput")},
        registered={"a/b": _Cls("a/b", inp="WrongInput", out="PutOutput")},
    )
    s = _find(findings, "schema_mismatch")
    assert s and "PutInput" in s[0].detail and "WrongInput" in s[0].detail


def test_a_matching_pair_produces_nothing():
    findings = check_operator_drift(
        declared={"a/b": _Meta(version="1.0", provides=["x.y"],
                               input_schema="In", output_schema="Out")},
        registered={"a/b": _Cls("a/b", version="1.0", capabilities=["x.y"],
                                inp="In", out="Out")},
    )
    assert findings == []


def test_unset_manifest_schema_is_not_drift():
    """Many manifests omit schema names; absence is not a contradiction."""
    findings = check_operator_drift(
        declared={"a/b": _Meta(input_schema=None, output_schema=None)},
        registered={"a/b": _Cls("a/b", inp="In", out="Out")},
    )
    assert _find(findings, "schema_mismatch") == []


def test_capability_order_does_not_matter():
    findings = check_operator_drift(
        declared={"a/b": _Meta(provides=["a", "b"])},
        registered={"a/b": _Cls("a/b", capabilities=["b", "a"])},
    )
    assert findings == []


# ── against the REAL installed tree ──────────────────────────────────────────

def test_real_tree_separates_not_installed_from_drift():
    """A pack on disk but not installed is NOT drift.

    Discovery walks the FILESYSTEM for pack.toml, but registration needs the pack
    pip-INSTALLED. Without this distinction every uninstalled pack would look like
    drift and the check would be useless as a CI gate (a dev machine or a slim
    deployment legitimately omits packs). The original case was `rag/parse`
    (retired in #2413).
    """
    from cairn.core.pack.drift import check_installed_operator_drift

    findings = check_installed_operator_drift()
    kinds = {f.kind for f in findings}

    assert "declared_not_registered" not in kinds, (
        "a not-installed pack must not be reported as drift: %r"
        % [f.detail for f in findings if f.kind == "declared_not_registered"]
    )
    not_installed = [f for f in findings if f.kind == "pack_not_installed"]
    if not_installed:
        assert all(not f.is_drift for f in not_installed)


def test_real_tree_has_no_manifest_drift():
    """The shipped tree is CLEAN — this is the gate.

    Three real capability drifts (`dataflow/run`, `ml/run`, `artifact/promote`
    declared capabilities in code but not in their manifests) were found and fixed
    by this check; it fails if any returns.
    """
    from cairn.core.pack.drift import check_installed_operator_drift

    real = [f for f in check_installed_operator_drift() if f.is_drift]
    assert real == [], "manifest/code drift: %r" % [f.detail for f in real]


def test_environment_facts_are_not_drift():
    from cairn.core.pack.drift import DriftFinding

    assert DriftFinding("pack_not_installed", "a/b", "…").is_drift is False
    assert DriftFinding("capability_mismatch", "a/b", "…").is_drift is True


def test_doctor_surfaces_drift(monkeypatch):
    from cairn.cli.commands import doctor

    monkeypatch.setattr(
        doctor, "check_installed_operator_drift",
        lambda: [DriftFinding("declared_not_registered", "a/b", "manifest declares a/b …")],
    )
    rows = doctor.check_manifest_drift()
    assert rows and rows[0].status == "fail" and "a/b" in rows[0].detail


def test_doctor_is_quiet_when_clean(monkeypatch):
    from cairn.cli.commands import doctor

    monkeypatch.setattr(doctor, "check_installed_operator_drift", lambda: [])
    rows = doctor.check_manifest_drift()
    assert len(rows) == 1 and rows[0].status == "ok"


@pytest.mark.parametrize("kind", [
    "declared_not_registered", "registered_not_declared",
    "version_mismatch", "capability_mismatch", "schema_mismatch",
])
def test_every_finding_kind_names_the_operator(kind):
    """A finding a reader cannot act on is noise — the operator must be named."""
    f = DriftFinding(kind, "some/op", "detail mentioning some/op")
    assert f.operator == "some/op" and f.operator in f.detail
