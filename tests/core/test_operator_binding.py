"""Tests for OperationBinding YAML loader (§24.20.5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cairn.core.operator.binding import (
    BindingError,
    OperationBinding,
    load_binding,
    load_bindings_from_dir,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _write_yaml(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ── Happy path — full §24.20.5 shape ─────────────────────────────────────


def test_full_binding_loads(tmp_path: Path):
    yaml_body = """
operator: clickstack/query_logs
version: ">= 0.1"

input:
  service: $state.alert.service
  start:   $state.alert.timeframe_start
  query:   "error rate spike"

output:
  logs: $state.logs
  count: $state.log_count

resources:
  cpu: 4
  memory_gb: 8
  timeout_minutes: 5

secrets:
  - CLICKSTACK_AUTH

artifacts:
  collect:
    - logs
    - summary

budget:
  max_cost_usd: 0.5
  estimated_cost_usd: 0.25

approval:
  required: true
  reason: "Cross-tenant query"
  channel: slack:#ops
  timeout_hours: 4

policy:
  apply:
    - data-residency
    - rate-limit

execution:
  profile: default
  mode: auto
""".strip()
    p = _write_yaml(tmp_path, "query.yaml", yaml_body)

    b = load_binding(p)

    assert b.operator == "clickstack/query_logs"
    assert b.version == ">= 0.1"
    assert b.input["service"] == "$state.alert.service"
    assert b.output["logs"] == "$state.logs"
    assert b.resources.cpu == 4
    assert b.resources.timeout_minutes == 5
    assert b.secrets == ["CLICKSTACK_AUTH"]
    assert b.artifacts.collect == ["logs", "summary"]
    assert b.budget.max_cost_usd == 0.5
    assert b.approval.required is True
    assert b.approval.timeout_hours == 4
    assert b.policy.apply == ["data-residency", "rate-limit"]
    assert b.execution.profile == "default"
    assert b.execution.mode == "auto"


# ── Minimum-viable binding (just operator + input) ───────────────────────


def test_minimum_viable_binding_loads(tmp_path: Path):
    yaml_body = """
operator: clickstack/query_logs
input:
  service: $state.service
""".strip()
    p = _write_yaml(tmp_path, "minimal.yaml", yaml_body)

    b = load_binding(p)
    assert b.operator == "clickstack/query_logs"
    assert b.input == {"service": "$state.service"}
    # All optional fields default to None or empty
    assert b.output == {}
    assert b.resources is None
    assert b.secrets == []
    assert b.budget is None
    assert b.approval is None


# ── Defaults for sub-models ──────────────────────────────────────────────


def test_approval_default_required_false(tmp_path: Path):
    yaml_body = """
operator: x/y
input: {a: 1}
approval:
  reason: "documented"
""".strip()
    p = _write_yaml(tmp_path, "approval.yaml", yaml_body)
    b = load_binding(p)
    assert b.approval.required is False
    assert b.approval.reason == "documented"


def test_execution_default_mode_auto(tmp_path: Path):
    yaml_body = """
operator: x/y
input: {a: 1}
execution:
  profile: gpu-training
""".strip()
    p = _write_yaml(tmp_path, "exec.yaml", yaml_body)
    b = load_binding(p)
    assert b.execution.profile == "gpu-training"
    assert b.execution.mode == "auto"  # default


# ── Error paths ──────────────────────────────────────────────────────────


def test_missing_file_raises_with_path(tmp_path: Path):
    missing = tmp_path / "nope.yaml"
    with pytest.raises(BindingError) as ei:
        load_binding(missing)
    assert str(missing) in str(ei.value)
    assert "not found" in str(ei.value)


def test_empty_file_raises(tmp_path: Path):
    p = _write_yaml(tmp_path, "empty.yaml", "")
    with pytest.raises(BindingError, match="empty"):
        load_binding(p)


def test_yaml_syntax_error_raises_with_path(tmp_path: Path):
    p = _write_yaml(tmp_path, "bad.yaml", "operator: foo\n  bad: indent\n")
    with pytest.raises(BindingError) as ei:
        load_binding(p)
    msg = str(ei.value)
    assert "bad.yaml" in msg
    # yaml lib message indicates parse failure
    assert "YAML parse error" in msg


def test_top_level_must_be_mapping(tmp_path: Path):
    p = _write_yaml(tmp_path, "list.yaml", "- a\n- b\n")
    with pytest.raises(BindingError, match="mapping at top level"):
        load_binding(p)


def test_missing_operator_field_raises(tmp_path: Path):
    yaml_body = "input: {a: 1}\n"
    p = _write_yaml(tmp_path, "noop.yaml", yaml_body)
    with pytest.raises(BindingError, match="schema validation"):
        load_binding(p)


def test_extra_top_level_field_rejected(tmp_path: Path):
    # §24.20.5 locks at 10 fields; 11th = soft-boundary breach. extra="forbid"
    yaml_body = """
operator: x/y
input: {a: 1}
custom_field: not_allowed
""".strip()
    p = _write_yaml(tmp_path, "extra.yaml", yaml_body)
    with pytest.raises(BindingError, match="schema validation"):
        load_binding(p)


def test_extra_subfield_in_resources_rejected(tmp_path: Path):
    yaml_body = """
operator: x/y
input: {a: 1}
resources:
  cpu: 4
  unknown: 100
""".strip()
    p = _write_yaml(tmp_path, "extra-res.yaml", yaml_body)
    with pytest.raises(BindingError, match="schema validation"):
        load_binding(p)


# ── Directory loader ─────────────────────────────────────────────────────


def test_load_bindings_from_dir_indexes_by_stem(tmp_path: Path):
    ops = tmp_path / "operations"
    ops.mkdir()
    _write_yaml(
        ops, "query-logs.yaml", "operator: clickstack/query_logs\ninput: {a: 1}\n"
    )
    _write_yaml(ops, "correlate.yaml", "operator: git/correlate\ninput: {a: 1}\n")

    bindings = load_bindings_from_dir(ops)

    assert set(bindings.keys()) == {"query-logs", "correlate"}
    assert bindings["query-logs"].operator == "clickstack/query_logs"
    assert bindings["correlate"].operator == "git/correlate"


def test_load_bindings_from_missing_dir_returns_empty(tmp_path: Path):
    assert load_bindings_from_dir(tmp_path / "nonexistent") == {}


def test_load_bindings_from_non_dir_raises(tmp_path: Path):
    f = _write_yaml(tmp_path, "notadir.yaml", "operator: x/y\ninput: {a: 1}\n")
    with pytest.raises(BindingError, match="non-directory"):
        load_bindings_from_dir(f)


def test_load_bindings_skips_non_yaml(tmp_path: Path):
    ops = tmp_path / "operations"
    ops.mkdir()
    _write_yaml(ops, "real.yaml", "operator: x/y\ninput: {a: 1}\n")
    # Non-yaml file should be ignored
    (ops / "README.md").write_text("# docs", encoding="utf-8")

    bindings = load_bindings_from_dir(ops)
    assert list(bindings.keys()) == ["real"]


def test_one_bad_binding_in_dir_fails_loud(tmp_path: Path):
    """A broken binding in operations/ MUST fail loud at load.

    Unlike the operator-pack loader (which keeps going to isolate broken
    packs from each other), operation bindings live INSIDE one domain
    pack and must be all-or-nothing.
    """
    ops = tmp_path / "operations"
    ops.mkdir()
    _write_yaml(ops, "good.yaml", "operator: x/y\ninput: {a: 1}\n")
    _write_yaml(ops, "bad.yaml", "no_operator: missing\n")

    with pytest.raises(BindingError):
        load_bindings_from_dir(ops)


# ── #2241 C: the v1 `capability` form (ADR-0111 P4/P8) ───────────────────────



@pytest.mark.parametrize("data,ok", [
    ({"capability": "contract://a.b/1.0.0"}, True),
    ({"capability": "contract://a.b/1.0.0", "configuration": {"k": 1}, "input": {"x": "$state.x"}}, True),
    ({"operator": "p/op"}, True),
    ({"capability": "contract://a.b/1.0.0", "version": ">=1"}, False),
    ({"capability": "contract://a.b/1.0.0", "secrets": ["K"]}, False),
    ({"capability": "contract://a.b/1.0.0", "execution": {"mode": "auto"}}, False),
    ({"capability": "contract://a.b/1"}, False),
    ({"operator": "p/op", "capability": "contract://a.b/1.0.0"}, False),
    ({"operator": "p/op", "configuration": {"k": 1}}, False),
    ({}, False),
])
def test_v1_operation_names_a_capability_and_nothing_a_provider_owns(data, ok):
    if ok:
        OperationBinding.model_validate(data)
    else:
        with pytest.raises(ValueError):
            OperationBinding.model_validate(data)
