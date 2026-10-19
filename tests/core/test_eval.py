"""Tests for the eval surface: scorer + dataset loading + CLI orchestration.

The scorer is the part that gets the most test attention — it's pure
logic and the contract that authors write against. Dataset loading is
strict on purpose so we exercise that. The `cairn eval` orchestrator left with
the command for the platform (#2421): packages/cairn-platform/tests/test_cli_commands.py.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import pytest

from cairn.core.eval import (
    EvalCase,
    EvalResult,
    EvalSummary,
    load_dataset,
    load_pack_datasets,
    score_case,
)


# ─── Scorer: subset semantics ─────────────────────────────────────────


def test_score_exact_match_returns_empty():
    """When expected == actual, no mismatches."""
    assert score_case({"a": 1}, {"a": 1}) == []


def test_score_extra_keys_in_actual_are_ok():
    """Extra paths in actual don't matter — subset semantics."""
    assert score_case(
        {"root_cause": {"category": "deploy"}},
        {"root_cause": {"category": "deploy", "confidence": 0.9},
         "evidence_count": 4},
    ) == []


def test_score_missing_key_in_actual_is_a_mismatch():
    mismatches = score_case({"a": {"b": 1}}, {"a": {}})
    assert any("a.b" in m and "missing" in m for m in mismatches)


def test_score_value_mismatch_reports_both_values():
    mismatches = score_case(
        {"root_cause": {"category": "deploy"}},
        {"root_cause": {"category": "network"}},
    )
    assert len(mismatches) == 1
    assert "root_cause.category" in mismatches[0]
    assert "deploy" in mismatches[0]
    assert "network" in mismatches[0]


def test_score_type_mismatch_at_dict_boundary():
    """Expected nested dict, got scalar → mismatch with type info."""
    mismatches = score_case({"a": {"b": 1}}, {"a": 5})
    assert len(mismatches) == 1
    assert "a:" in mismatches[0]
    assert "dict" in mismatches[0]


def test_score_list_compared_by_index():
    mismatches = score_case(
        {"items": [{"name": "x"}, {"name": "y"}]},
        {"items": [{"name": "x"}, {"name": "z"}]},
    )
    assert len(mismatches) == 1
    assert "items[1].name" in mismatches[0]


def test_score_list_with_more_actual_entries_is_ok():
    """Expected can specify the first N — extras in actual are fine."""
    assert score_case({"items": [{"k": 1}]}, {"items": [{"k": 1}, {"k": 2}]}) == []


def test_score_list_with_fewer_actual_entries_reports_missing():
    mismatches = score_case(
        {"items": [{"k": 1}, {"k": 2}]},
        {"items": [{"k": 1}]},
    )
    assert any("items[1]" in m and "missing" in m for m in mismatches)


def test_score_multiple_mismatches_all_reported():
    """One mismatch shouldn't short-circuit the rest."""
    mismatches = score_case(
        {"a": 1, "b": 2, "c": {"d": "x"}},
        {"a": 99, "c": {"d": "y"}},
    )
    paths = " ".join(mismatches)
    assert "a:" in paths
    assert "b" in paths and "missing" in paths
    assert "c.d" in paths


# ─── Dataset loading: strict + tolerant of blanks/comments ────────────


def _write_dataset(tmp_path: Path, name: str, rows: list[dict]) -> Path:
    p = tmp_path / f"{name}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def test_load_dataset_reads_each_row(tmp_path):
    p = _write_dataset(tmp_path, "cases", [
        {"name": "c1", "trigger": {"source": "manual"}, "expected": {}},
        {"name": "c2", "trigger": {"source": "manual"}, "expected": {"x": 1}},
    ])
    cases = load_dataset(p)
    assert len(cases) == 2
    assert cases[0].name == "c1"
    assert cases[1].expected == {"x": 1}


def test_load_dataset_ignores_blank_lines_and_hash_comments(tmp_path):
    p = tmp_path / "cases.jsonl"
    p.write_text(
        "\n"
        "# this is a comment\n"
        '{"name": "c1", "trigger": {"source": "manual"}, "expected": {}}\n'
        "\n"
    )
    cases = load_dataset(p)
    assert len(cases) == 1


def test_load_dataset_strict_on_invalid_json(tmp_path):
    """A malformed line raises immediately — authors find typos at load,
    not after 99 cases run."""
    p = tmp_path / "cases.jsonl"
    p.write_text(
        '{"name": "ok", "trigger": {"source": "manual"}, "expected": {}}\n'
        '{this is not json\n'
    )
    with pytest.raises(ValueError, match="invalid JSON"):
        load_dataset(p)


def test_load_dataset_strict_on_invalid_case_shape(tmp_path):
    """Wrong trigger shape → Pydantic raises → wrapped with line info."""
    p = tmp_path / "cases.jsonl"
    p.write_text('{"name": "bad", "trigger": {"source": "NOT_A_VALID_SOURCE"}}\n')
    with pytest.raises(ValueError, match="invalid case shape"):
        load_dataset(p)


def test_load_pack_datasets_discovers_jsonl_files(tmp_path):
    """Returns dict keyed by file stem so --dataset can pick one."""
    _write_dataset(tmp_path, "smoke", [{"name": "c1", "trigger": {"source": "manual"}}])
    _write_dataset(tmp_path, "regression", [
        {"name": "c2", "trigger": {"source": "manual"}}
    ])
    # A non-jsonl file should be ignored
    (tmp_path / "readme.md").write_text("not a dataset\n")

    datasets = load_pack_datasets(tmp_path)
    assert set(datasets.keys()) == {"smoke", "regression"}
    assert len(datasets["smoke"]) == 1


def test_load_pack_datasets_missing_dir_returns_empty(tmp_path):
    assert load_pack_datasets(tmp_path / "nope") == {}


def test_eval_summary_all_pass_property():
    s = EvalSummary(total=3, passed=3, failed=0, errored=0)
    assert s.all_pass is True

    s = EvalSummary(total=3, passed=2, failed=1, errored=0)
    assert s.all_pass is False

    s = EvalSummary(total=3, passed=2, failed=0, errored=1)
    assert s.all_pass is False
