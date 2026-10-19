"""Tests for validate_operation_binding (SCH4 #192)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from cairn.core.operator import (
    Operation,
    OperationBinding,
    OperationBindingError,
    validate_operation_binding,
)


# ── Operator + schema fixtures ───────────────────────────────────────────


class _QueryIn(BaseModel):
    service: str
    start_time: str
    end_time: str
    limit: int = 100  # optional — has default


class _QueryOut(BaseModel):
    logs: list[str]
    summary: str


class _QueryLogsOp(Operation[_QueryIn, _QueryOut]):
    name = "clickstack/query_logs"
    version = "0.1.0"
    input_schema = _QueryIn
    output_schema = _QueryOut

    def run(self, inp, ctx):  # pragma: no cover - not exercised here
        return _QueryOut(logs=[], summary="")


# ── Happy paths ──────────────────────────────────────────────────────────


def test_valid_binding_passes():
    binding = OperationBinding(
        operator="clickstack/query_logs",
        input={
            "service": "$state.alert.service",
            "start_time": "$state.alert.start",
            "end_time": "$state.alert.end",
        },
        output={"logs": "$state.logs", "summary": "$state.summary"},
    )
    # No raise
    validate_operation_binding(binding, _QueryLogsOp, site="test")


def test_optional_input_field_may_be_omitted():
    """``limit`` has a default → may be left out of the binding."""
    binding = OperationBinding(
        operator="clickstack/query_logs",
        input={
            "service": "$state.alert.service",
            "start_time": "$state.alert.start",
            "end_time": "$state.alert.end",
            # limit omitted on purpose
        },
        output={"logs": "$state.logs", "summary": "$state.summary"},
    )
    validate_operation_binding(binding, _QueryLogsOp, site="test")


def test_optional_input_field_may_be_provided():
    binding = OperationBinding(
        operator="clickstack/query_logs",
        input={
            "service": "$state.alert.service",
            "start_time": "$state.alert.start",
            "end_time": "$state.alert.end",
            "limit": 500,
        },
        output={"logs": "$state.logs", "summary": "$state.summary"},
    )
    validate_operation_binding(binding, _QueryLogsOp, site="test")


def test_partial_output_capture_is_allowed():
    """Output binding only captures some operator outputs — the rest get dropped."""
    binding = OperationBinding(
        operator="clickstack/query_logs",
        input={
            "service": "$state.alert.service",
            "start_time": "$state.alert.start",
            "end_time": "$state.alert.end",
        },
        output={"logs": "$state.logs"},  # summary intentionally dropped
    )
    validate_operation_binding(binding, _QueryLogsOp, site="test")


def test_empty_output_is_allowed():
    """Some operators are void; output binding may be empty."""
    binding = OperationBinding(
        operator="clickstack/query_logs",
        input={
            "service": "$state.alert.service",
            "start_time": "$state.alert.start",
            "end_time": "$state.alert.end",
        },
        # output omitted
    )
    validate_operation_binding(binding, _QueryLogsOp, site="test")


# ── Input violations ─────────────────────────────────────────────────────


def test_missing_required_input_field_raises():
    binding = OperationBinding(
        operator="clickstack/query_logs",
        input={
            "service": "$state.alert.service",
            "start_time": "$state.alert.start",
            # end_time missing — required
        },
        output={"logs": "$state.logs", "summary": "$state.summary"},
    )
    with pytest.raises(OperationBindingError) as ei:
        validate_operation_binding(
            binding, _QueryLogsOp, site="operations/q.yaml"
        )
    msg = str(ei.value)
    assert "end_time" in msg
    assert "_QueryIn" in msg
    assert "operations/q.yaml" in msg  # site surfaces
    assert "clickstack/query_logs" in msg
    # Known-fields hint helps the author
    assert "service" in msg


def test_multiple_missing_required_fields_are_all_listed():
    binding = OperationBinding(
        operator="clickstack/query_logs",
        input={"service": "$state.alert.service"},
        # start_time AND end_time both missing
        output={"logs": "$state.logs", "summary": "$state.summary"},
    )
    with pytest.raises(OperationBindingError) as ei:
        validate_operation_binding(binding, _QueryLogsOp, site="test")
    msg = str(ei.value)
    assert "start_time" in msg
    assert "end_time" in msg


def test_unknown_input_field_raises():
    """Typo: ``servce`` instead of ``service``."""
    binding = OperationBinding(
        operator="clickstack/query_logs",
        input={
            "servce": "$state.alert.service",  # typo
            "start_time": "$state.alert.start",
            "end_time": "$state.alert.end",
        },
        output={"logs": "$state.logs", "summary": "$state.summary"},
    )
    with pytest.raises(OperationBindingError) as ei:
        validate_operation_binding(binding, _QueryLogsOp, site="test")
    msg = str(ei.value)
    assert "servce" in msg
    assert "unknown input field" in msg


# ── Output violations ────────────────────────────────────────────────────


def test_unknown_output_field_raises():
    """Capturing a field the operator never emits is a bug."""
    binding = OperationBinding(
        operator="clickstack/query_logs",
        input={
            "service": "$state.alert.service",
            "start_time": "$state.alert.start",
            "end_time": "$state.alert.end",
        },
        output={"never_emitted": "$state.x"},
    )
    with pytest.raises(OperationBindingError) as ei:
        validate_operation_binding(binding, _QueryLogsOp, site="test")
    msg = str(ei.value)
    assert "never_emitted" in msg
    assert "_QueryOut" in msg
    # known fields hint
    assert "logs" in msg
    assert "summary" in msg


# ── Site labelling ───────────────────────────────────────────────────────


def test_site_appears_in_every_error_message():
    binding = OperationBinding(
        operator="clickstack/query_logs",
        input={},  # everything missing
        output={"logs": "$state.logs", "summary": "$state.summary"},
    )
    with pytest.raises(OperationBindingError) as ei:
        validate_operation_binding(
            binding, _QueryLogsOp, site="MY_CUSTOM_SITE_LABEL"
        )
    assert "MY_CUSTOM_SITE_LABEL" in str(ei.value)


# ── Operator without schemas (defensive) ─────────────────────────────────


def test_validator_skips_when_input_schema_is_none():
    """An operator with no declared input_schema is unusual but legal —
    nothing to validate against, so the validator is a no-op for inputs."""

    class _VoidIn(BaseModel):
        pass

    class _Out(BaseModel):
        ok: bool

    class _SchemalessOp(Operation[_VoidIn, _Out]):
        name = "test/void"
        version = "0.0.1"
        input_schema = None  # type: ignore[assignment]
        output_schema = _Out

        def run(self, inp, ctx):  # pragma: no cover
            return _Out(ok=True)

    binding = OperationBinding(
        operator="test/void",
        input={"anything": 1},
        output={"ok": "$state.ok"},
    )
    # Should pass (input check skipped; output check still runs and ok is valid)
    validate_operation_binding(binding, _SchemalessOp, site="test")
