"""ADR-0074 — shared data-validation services: loaders + profile + quality."""
from __future__ import annotations

import json

import pytest

from cairn.services.data import (
    DataExpectation,
    Expectation,
    ExpectationSuite,
    Role,
    detect_dataset_format,
    load_records,
    profile_data,
    run_expectations,
)


# ── loaders (tier 1: format/parse) ───────────────────────────────────────────
def test_load_jsonl(tmp_path):
    p = tmp_path / "d.jsonl"
    p.write_text('{"input":"hi","output":"hello"}\n\n{"input":"bye","output":"ciao"}\n')
    rows = load_records(str(p))
    assert len(rows) == 2 and rows[0] == {"input": "hi", "output": "hello"}


def test_load_csv_drops_empty_cells(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("input,output\nhi,hello\nbye,\n")
    rows = load_records(str(p))
    assert rows[0] == {"input": "hi", "output": "hello"}
    assert rows[1] == {"input": "bye"}  # empty output cell dropped


def test_load_json_array_and_wrapper(tmp_path):
    p = tmp_path / "a.json"
    p.write_text(json.dumps([{"a": 1}, {"a": 2}]))
    assert load_records(str(p)) == [{"a": 1}, {"a": 2}]
    p2 = tmp_path / "w.json"
    p2.write_text(json.dumps({"data": [{"a": 1}]}))
    assert load_records(str(p2)) == [{"a": 1}]


def test_load_file_uri_and_missing(tmp_path):
    p = tmp_path / "d.jsonl"
    p.write_text('{"x":1}\n')
    assert load_records(f"file://{p}") == [{"x": 1}]
    with pytest.raises(FileNotFoundError):
        load_records(str(tmp_path / "nope.jsonl"))


def test_detect_dataset_format():
    assert detect_dataset_format([{"messages": [{"role": "user", "content": "hi"}]}]) == "openai_chat"
    assert detect_dataset_format([{"instruction": "do", "output": "done"}]) == "alpaca"
    assert detect_dataset_format([{"prompt": "p", "completion": "c"}]) == "completion"
    assert detect_dataset_format([{"foo": "bar"}]) == "unknown"


# ── profile (tier 2: semantic roles) ─────────────────────────────────────────
EVAL_EXP = DataExpectation(kind="rows", roles=[
    Role(name="input", required=True, aliases=["prompt", "question"]),
    Role(name="output", required=True, aliases=["answer", "completion"]),
    Role(name="reference", required=False, enables="correctness scoring"),
])


def test_profile_pass_all_roles_resolved():
    # includes the optional `reference` role → a true clean PASS
    rows = [{"input": "hi", "output": "hello", "reference": "hello"},
            {"input": "bye", "output": "ciao", "reference": "ciao"}]
    prof = profile_data(rows, EVAL_EXP)
    assert prof.verdict == "pass" and prof.ok
    assert not prof.missing_required and not prof.degraded


def test_profile_block_missing_required_role():
    rows = [{"input": "hi"}, {"input": "bye"}]  # no output/answer/completion anywhere
    prof = profile_data(rows, EVAL_EXP)
    assert prof.verdict == "block" and not prof.ok
    assert "output" in prof.missing_required


def test_profile_resolves_via_alias():
    rows = [{"prompt": "hi", "answer": "hello"}]  # aliases resolve the REQUIRED roles
    prof = profile_data(rows, EVAL_EXP)
    assert prof.verdict != "block" and not prof.missing_required  # required roles satisfied via alias


def test_profile_degrade_on_optional_role():
    rows = [{"input": "hi", "output": "hello"}]  # required present, reference absent
    prof = profile_data(rows, EVAL_EXP)
    assert prof.verdict == "degrade"
    assert "correctness scoring" in prof.degraded


def test_profile_empty_blocks():
    assert profile_data([], EVAL_EXP).verdict == "block"


def test_profile_dataset_format_family_gate():
    # dataset (fine-tuning) rows: the FORMAT is the requirement, not flat roles
    exp = DataExpectation(kind="rows", format_families=["openai_chat", "alpaca", "completion"])
    chat = [{"messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]}]
    assert profile_data(chat, exp).verdict == "pass"           # recognized chat format
    assert profile_data([{"foo": "bar"}], exp).verdict == "block"  # unrecognized → won't train


def test_profile_spans_field_mapping_degrade():
    exp = DataExpectation(kind="spans", roles=[
        Role(name="service", required=True), Role(name="duration_ms", required=True),
        Role(name="parent_span_id", required=False, enables="dependency graph"),
    ])
    rows = [{"service": "checkout", "duration_ms": 12.0}]
    prof = profile_data(rows, exp)
    assert prof.verdict == "degrade" and "dependency graph" in prof.degraded


def test_profile_unstructured_needs_only_content():
    exp = DataExpectation(kind="documents", unstructured=True)
    assert profile_data([{"text": "hello world"}], exp).verdict == "pass"
    assert profile_data([{"nothing": ""}], exp).verdict == "block"


# ── profile (tier 3a: per-record structural, strict) ─────────────────────────
def test_strict_rows_blocks_malformed_chat():
    exp = DataExpectation(kind="rows", strictness="strict", roles=[Role(name="messages", required=True)])
    good = [{"messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]}]
    bad = [{"messages": [{"role": "user", "content": ""}]}]  # empty content
    assert profile_data(good, exp).verdict == "pass"
    assert profile_data(bad, exp).verdict == "block"


def test_strict_record_schema_blocks():
    schema = {"type": "object", "required": ["label"], "properties": {"label": {"type": "string"}}}
    exp = DataExpectation(kind="rows", strictness="strict", record_schema=schema,
                          roles=[Role(name="label", required=True)])
    assert profile_data([{"label": "ok"}], exp).verdict == "pass"
    prof = profile_data([{"label": 3}], exp)  # wrong type
    assert prof.verdict == "block" and any("record_schema" in e for e in prof.errors)


# ── quality (tier 3b: expectations suite) ────────────────────────────────────
def test_expectations_pass():
    suite = ExpectationSuite(name="s", expectations=[
        Expectation(type="row_count", min=1),
        Expectation(type="not_null", column="label"),
        Expectation(type="in_set", column="label", values=["a", "b"]),
    ])
    rep = run_expectations([{"label": "a"}, {"label": "b"}], suite)
    assert rep.verdict == "pass" and rep.failed == 0 and rep.engine == "builtin"


def test_expectations_block_on_nulls_and_range():
    suite = ExpectationSuite(expectations=[
        Expectation(type="not_null", column="score"),
        Expectation(type="in_range", column="score", min=0, max=1),
    ])
    rep = run_expectations([{"score": 0.5}, {"score": None}, {"score": 2.0}], suite)
    assert rep.verdict == "block" and rep.failed == 2


def test_expectations_unique_and_regex():
    suite = ExpectationSuite(expectations=[
        Expectation(type="unique", column="id"),
        Expectation(type="matches_regex", column="email", pattern=r"@"),
    ])
    rep = run_expectations([{"id": 1, "email": "a@x.com"}, {"id": 1, "email": "nope"}], suite)
    assert rep.verdict == "block" and rep.failed == 2


def test_expectations_warn_severity_degrades():
    suite = ExpectationSuite(expectations=[
        Expectation(type="row_count", min=100, severity="warn"),
    ])
    rep = run_expectations([{"a": 1}], suite)
    assert rep.verdict == "degrade"  # warn-only failure
