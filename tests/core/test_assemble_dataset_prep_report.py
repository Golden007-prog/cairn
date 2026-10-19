"""Unit tests for the ``assemble_dataset_prep_report`` python_node (#711).

The dataset-prep template ends in this deterministic node instead of an LLM
synthesize — it reads the ``findings`` the prep operators wrote and re-emits the
DatasetPrepReport fields at TOP-LEVEL (so the report is complete + no API key is
needed to run the flow). These tests pin that mapping + the summary.
"""

from __future__ import annotations

import asyncio

from cairn.core.engine.python_function_registry import (
    known_functions,
    resolve_python_function,
)


def _run(state: dict) -> dict:
    fn = resolve_python_function("assemble_dataset_prep_report")
    return asyncio.run(fn(dict(state)))


def test_registered():
    assert "assemble_dataset_prep_report" in known_functions()


def test_hoists_prep_findings_to_top_level():
    state = {
        "findings": {
            "prep_anon_path": "/tmp/cairn-prep/anonymized.jsonl",
            "prep_redacted": 12,
            "prep_total": 48,
            "prep_split_paths": {
                "train": "/tmp/s/train.jsonl",
                "val": "/tmp/s/val.jsonl",
                "test": "/tmp/s/test.jsonl",
            },
            "prep_no_leakage": True,
            "prep_split_counts": {"train": 38, "val": 5, "test": 5},
            "prep_anon_backend": "presidio",
            "prov_ok": True,
        }
    }
    out = _run(state)
    assert out["anonymized_path"] == "/tmp/cairn-prep/anonymized.jsonl"
    assert out["redacted_rows"] == 12
    assert out["total_rows"] == 48
    assert out["split_paths"]["test"] == "/tmp/s/test.jsonl"  # held-out test present (#9)
    assert out["no_leakage"] is True
    assert out["split_counts"] == {"train": 38, "val": 5, "test": 5}
    assert out["provenance_ok"] is True
    # summary is a deterministic fact line
    assert "Prepped 48 rows" in out["summary"]
    assert "redacted 12" in out["summary"]
    assert "leakage-free=True" in out["summary"]
    assert out["pii_backend"] == "presidio"
    assert out["warnings"] == []  # presidio → no degradation warning


def test_regex_backend_emits_a_governance_warning():
    # A run that silently degraded to the zero-dep regex backend must WARN — a
    # "PII-scrubbed" dataset that only caught email/secrets is a governance lie.
    out = _run({"findings": {"prep_anon_backend": "regex", "prep_total": 10}})
    assert out["pii_backend"] == "regex"
    assert len(out["warnings"]) == 1
    assert "regex" in out["warnings"][0] and "phone" in out["warnings"][0].lower()
    assert "warning" in out["summary"].lower()


def test_empty_state_is_safe():
    out = _run({})
    assert out["redacted_rows"] == 0
    assert out["total_rows"] == 0
    assert out["split_paths"] == {}
    assert out["no_leakage"] is False
    assert out["summary"]  # never empty (schema requires it)


# ── #870 — the POST-SCRUB PII invariant (re-scan of the scrubbed output) ──────


def _base_findings(**over) -> dict:
    f = {
        "prep_anon_path": "/tmp/cairn-prep/anonymized.jsonl",
        "prep_redacted": 12, "prep_total": 48,
        "prep_split_paths": {"train": "/t/train.jsonl", "val": "/t/val.jsonl", "test": "/t/test.jsonl"},
        "prep_no_leakage": True, "prep_split_counts": {"train": 38, "val": 5, "test": 5},
        "prep_anon_backend": "presidio", "prov_ok": True,
    }
    f.update(over)
    return f


def test_post_scrub_clean_when_no_residual_pii():
    out = _run({"findings": _base_findings(post_scrub_pii_rows=0)})
    assert out["post_scrub_pii_rows"] == 0
    assert out["post_scrub_pii_clean"] is True
    assert not any("INVARIANT FAILED" in w for w in out["warnings"])
    assert "post-scrub-clean=True" in out["summary"]


def test_post_scrub_dirty_fails_invariant_loudly():
    out = _run({"findings": _base_findings(post_scrub_pii_rows=3)})
    assert out["post_scrub_pii_rows"] == 3
    assert out["post_scrub_pii_clean"] is False
    # the scrubbed artifact that would train still has PII → a loud, actionable warning
    assert any("POST-SCRUB PII INVARIANT FAILED" in w and "3 row" in w for w in out["warnings"])


def test_post_scrub_absent_defaults_to_clean():
    # older runs / no verify step → treated as clean (0), never a spurious failure.
    out = _run({"findings": _base_findings()})
    assert out["post_scrub_pii_rows"] == 0 and out["post_scrub_pii_clean"] is True
    assert out["prep_gate_verdict"] == "pass"      # #882 — defaults pass pre-gate


def test_prep_gate_verdict_surfaced_on_report():
    # #882 — the gate's verdict is hoisted onto the report (a consumer gates on it).
    out = _run({"findings": _base_findings(post_scrub_pii_rows=3, prep_gate_verdict="fail")})
    assert out["prep_gate_verdict"] == "fail"


# ── #889/#890 — certified-clean provenance on the report ──────────────────────


def test_verified_true_when_full_presidio_and_clean():
    out = _run({"findings": _base_findings(post_scrub_pii_rows=0, prep_gate_verified=True,
                                           post_scrub_pii_backend="presidio")})
    assert out["post_scrub_pii_verified"] is True
    assert out["post_scrub_pii_backend"] == "presidio"
    assert not any("NOT CERTIFIED" in w for w in out["warnings"])
    assert "certified=True" in out["summary"]


def test_clean_but_uncertified_warns_loudly():
    # 0 PII found but prep_gate did not verify (regex/sample) → a loud NOT-CERTIFIED warning.
    out = _run({"findings": _base_findings(post_scrub_pii_rows=0, prep_gate_verified=False,
                                           post_scrub_pii_backend="regex", post_scrub_pii_sampled=False)})
    assert out["post_scrub_pii_clean"] is True and out["post_scrub_pii_verified"] is False
    assert any("NOT CERTIFIED" in w and "presidio" in w for w in out["warnings"])
