"""#898 — the `production` profile + the doctor production-readiness guard.

The profile flips the governed gates to their fail-closed settings; `cairn doctor`
cross-checks that declared intent against what's actually installed, so a governed
run can't silently degrade to a weaker backend than the profile promised
(stub / regex / skipped-benchmark). Presidio + lm-eval are NOT installed in the
hermetic test venv, so the check is expected to FAIL under the production profile —
which is exactly the guard working.

The `production` profile itself is the platform's (ADR-0134 D5): its settings are pinned by
the platform's tests. The doctor guard is public, so it is proved here against a profile
that declares the same intent.
"""
from __future__ import annotations

import importlib.util

import pytest

from cairn.cli.commands.doctor import check_production_readiness


@pytest.fixture
def _prod(tmp_path):
    """A profile declaring what the platform's `production.toml` declares: the fail-closed promote
    gate (#892/#895/#898) and the presidio PII scan (#889)."""
    p = tmp_path / "production.toml"
    p.write_text('[profile]\n'
                 '[profile.config."model/gate".thresholds]\n'
                 'allow_stub = false\nblock_on_regression = true\nrequire_benchmark = true\n'
                 '[profile.config."dataset/pii_scan"]\nbackend = "presidio"\n', encoding="utf-8")
    return p


def test_doctor_is_a_noop_without_a_profile(monkeypatch):
    monkeypatch.delenv("OBS_PROFILE", raising=False)
    assert check_production_readiness() == []  # dev — nothing declared, nothing to enforce


def test_doctor_fails_when_profile_demands_missing_backends(monkeypatch, _prod):
    # Under the production profile, presidio + lm-eval are required. They aren't
    # installed in the hermetic venv, so the guard must FAIL loud (the whole point).
    monkeypatch.setenv("OBS_PROFILE", str(_prod))
    results = check_production_readiness()
    by_name = {r.name: r for r in results}

    if importlib.util.find_spec("presidio_analyzer") is None:
        assert by_name["pii-backend"].status == "fail"
        assert "presidio" in by_name["pii-backend"].detail
    if importlib.util.find_spec("lm_eval") is None:
        assert by_name["benchmark-harness"].status == "fail"
    # every emitted result is filed under the 'production' category
    assert results and all(r.category == "production" for r in results)


def test_doctor_temporal_runtime_needs_temporalio(monkeypatch, _prod):
    monkeypatch.setenv("OBS_PROFILE", str(_prod))
    monkeypatch.setenv("OBS_RUNTIME", "temporal")
    names = {r.name: r for r in check_production_readiness()}
    if importlib.util.find_spec("temporalio") is None:
        assert names["runtime-temporal"].status == "fail"
