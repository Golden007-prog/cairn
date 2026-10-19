"""Tests for the Operation Protocol + OperationContext + Null store impls.

Covers OP1 (#180) acceptance:
  - Operation subclass with Pydantic schemas works under typing.
  - OperationContext default field shape matches §24.20.13.
  - Null store implementations behave correctly (no-op, raise on
    undeclared secret, ledger accumulates).
"""

from __future__ import annotations

import logging

import pytest
from pydantic import BaseModel

from cairn.core.operator import (
    NullArtifactStore,
    NullCostLedger,
    NullMetricsLogger,
    NullSecretStore,
    Operation,
    OperationContext,
    ResourceSpec,
)


# ── Test fixtures: a minimal operator ────────────────────────────────────


class _DemoInput(BaseModel):
    value: int


class _DemoOutput(BaseModel):
    doubled: int


class _DemoOp(Operation[_DemoInput, _DemoOutput]):
    name = "demo/double"
    version = "0.0.1"
    input_schema = _DemoInput
    output_schema = _DemoOutput

    def run(self, inp: _DemoInput, ctx: OperationContext) -> _DemoOutput:
        return _DemoOutput(doubled=inp.value * 2)


# ── Operation Protocol shape ─────────────────────────────────────────────


def test_operation_subclass_has_required_attributes():
    assert _DemoOp.name == "demo/double"
    assert _DemoOp.version == "0.0.1"
    assert _DemoOp.input_schema is _DemoInput
    assert _DemoOp.output_schema is _DemoOutput


def test_operation_default_metadata_flags():
    # All class-level flags default to False/None per §24.20.12
    assert _DemoOp.long_running is False
    assert _DemoOp.requires_gpu is False
    assert _DemoOp.supports_cancel is False
    assert _DemoOp.supports_heartbeat is False
    assert _DemoOp.supports_mock is False
    assert _DemoOp.image is None
    assert _DemoOp.resources is None


def test_operation_run_executes_with_null_context():
    op = _DemoOp()
    ctx = OperationContext(pipeline_id="p1", node_name="n1")
    out = op.run(_DemoInput(value=21), ctx)
    assert isinstance(out, _DemoOutput)
    assert out.doubled == 42


def test_operation_base_run_raises_not_implemented():
    class _Bare(Operation[_DemoInput, _DemoOutput]):
        name = "demo/bare"
        version = "0.0.1"
        input_schema = _DemoInput
        output_schema = _DemoOutput

    ctx = OperationContext(pipeline_id="p1", node_name="n1")
    with pytest.raises(NotImplementedError):
        _Bare().run(_DemoInput(value=1), ctx)


def test_operation_optional_methods_default_safely():
    """mock/cancel/heartbeat have safe defaults when not overridden."""
    op = _DemoOp()
    ctx = OperationContext(pipeline_id="p1", node_name="n1")

    # mock raises NotImplementedError (supports_mock=False is the cue)
    with pytest.raises(NotImplementedError):
        op.mock(_DemoInput(value=1), ctx)

    # cancel + heartbeat are no-ops
    assert op.cancel(ctx) is None
    assert op.heartbeat(ctx) == {}


# ── OperationContext (§24.20.13) ─────────────────────────────────────────


def test_operation_context_default_shape():
    ctx = OperationContext(pipeline_id="p", node_name="n")
    # Identity
    assert ctx.pipeline_id == "p"
    assert ctx.node_name == "n"
    assert ctx.is_mocked is False
    # Service handles default to Null impls
    assert isinstance(ctx.artifacts, NullArtifactStore)
    assert isinstance(ctx.secrets, NullSecretStore)
    assert isinstance(ctx.metrics, NullMetricsLogger)
    assert isinstance(ctx.cost, NullCostLedger)
    # Logger
    assert isinstance(ctx.logger, logging.Logger)


def test_operation_context_heartbeat_and_cancel_default_noop():
    ctx = OperationContext(pipeline_id="p", node_name="n")
    assert ctx.emit_heartbeat({"step": 1}) is None
    assert ctx.check_cancelled() is False


# ── Null store implementations ───────────────────────────────────────────


def test_null_artifact_store_roundtrip():
    store = NullArtifactStore()
    uri = store.put("checkpoint", "s3://bucket/key")
    assert uri == "s3://bucket/key"
    assert store.get("checkpoint") == "s3://bucket/key"
    assert store.get("missing") is None


def test_null_secret_store_raises_on_get():
    store = NullSecretStore()
    assert store.available_keys() == []
    with pytest.raises(KeyError):
        store.get("HF_TOKEN")


def test_null_metrics_logger_swallows_calls():
    logger = NullMetricsLogger()
    assert logger.log("key", 42) is None  # no side effects


def test_null_cost_ledger_accumulates_but_reports_zero_initially():
    ledger = NullCostLedger()
    assert ledger.consumed() == 0.0
    ledger.charge(1.50, reason="api")
    ledger.charge(0.25)
    assert ledger.consumed() == 1.75


# ── ResourceSpec (§24.20.5) ──────────────────────────────────────────────


def test_resource_spec_defaults_all_none():
    spec = ResourceSpec()
    assert spec.cpu is None
    assert spec.memory_gb is None
    assert spec.gpu_count is None
    assert spec.gpu_type is None
    assert spec.disk_gb is None
    assert spec.timeout_minutes is None


def test_resource_spec_frozen():
    spec = ResourceSpec(cpu=8, gpu_count=1, gpu_type="A100")
    with pytest.raises(Exception):
        # frozen=True → setting an attribute raises FrozenInstanceError
        spec.cpu = 16  # type: ignore[misc]
