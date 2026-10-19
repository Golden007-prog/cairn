"""`cairn gate` — the CI verdict + exit-code contract (#990, ADR-0076 D2).

Hermetic: the verdict extraction + exit-code mapping are pure functions of a
run's output, so we pin them directly (no engine/pack load). The end-to-end run
(clean fixture → PASS/0, dirty fixture → FAIL/1) is exercised by the pack's own
operator tests + verified live; here we lock the CLI's classification contract,
which is what CI gates on.
"""
from __future__ import annotations

import json

import pytest

from cairn.cli.commands.gate import (
    _classify,
    _exit_code,
    _extract_verdict,
    _load_inputs,
)


@pytest.mark.parametrize("raw,expected", [
    ("FAIL", "fail"), ("fail", "fail"), ("blocked", "fail"), ("reject", "fail"),
    ("WARN", "warn"), ("degraded", "warn"),
    ("pass", "pass"), ("PASS", "pass"), ("ready", "pass"), ("", "pass"),
])
def test_classify(raw, expected):
    assert _classify(raw) == expected


def test_extract_verdict_top_level_scalar():
    # dataset-readiness writes a top-level `verdict` scalar + blocking list.
    out = {"verdict": "fail", "blocking": ["leaked key in row 3"], "warnings": []}
    v = _extract_verdict(out)
    assert v["verdict"] == "fail"
    assert v["label"] == "FAIL"
    assert v["blocking"] == ["leaked key in row 3"]


def test_extract_verdict_outcome_channel_head_and_reasons():
    # the ADR-0075 outcome channel: a verdict head + cited non-ok checks.
    out = {"outcome": [
        {"name": "readiness", "value": "FAIL"},
        {"name": "PII rows", "value": 7, "severity": "warn"},
        {"name": "format conformant", "value": 98.1, "severity": "block"},
        {"name": "duplicates", "value": 0.0, "severity": "ok"},  # ok → not a reason
    ]}
    v = _extract_verdict(out)
    assert v["verdict"] == "fail"
    names = {r["name"] for r in v["reasons"]}
    assert names == {"PII rows", "format conformant"}  # the "ok" check is excluded


def test_extract_verdict_defaults_to_pass_when_no_signal():
    assert _extract_verdict({})["verdict"] == "pass"
    assert _extract_verdict({"something": 1})["verdict"] == "pass"


def test_exit_code_fail_on_fail():
    assert _exit_code("pass", "fail") == 0
    assert _exit_code("warn", "fail") == 0   # a warning does NOT block by default
    assert _exit_code("fail", "fail") == 1


def test_exit_code_fail_on_warn():
    assert _exit_code("pass", "warn") == 0
    assert _exit_code("warn", "warn") == 1   # --fail-on warn makes a warning block
    assert _exit_code("fail", "warn") == 1


def test_load_inputs_merges_file_and_set(tmp_path):
    p = tmp_path / "in.json"
    p.write_text(json.dumps({"path": "data/a.jsonl", "format": "trl"}))
    merged = _load_inputs(str(p), ["format=axolotl", "extra=1"])
    assert merged == {"path": "data/a.jsonl", "format": "axolotl", "extra": "1"}  # --set overrides


def test_load_inputs_rejects_bad_set():
    with pytest.raises(ValueError):
        _load_inputs(None, ["missing-equals"])


def test_load_inputs_missing_file():
    with pytest.raises(ValueError):
        _load_inputs("/no/such/file.json", None)
