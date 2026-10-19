"""Tests for the runtime layer — NodeKind tags, RetryPolicy resolution,
select_runtime() lazy import, and the Temporal subpackage's availability gate.

The Temporal path itself isn't exercised here (it requires a live cluster).
We test:
  - tag() stamps both kind and retry; get_kind/get_retry round-trip
  - retry.resolve() merges per-template overrides over per-kind defaults
  - select_runtime() returns InProcessRuntime by default
  - select_runtime("temporal") raises a clear ImportError when temporalio is
    not installed (the live worker path is covered by an integration test
    once a real cluster is wired up)
"""

from __future__ import annotations

import pytest

from cairn.core.runtime import (
    InProcessRuntime,
    NodeKind,
    get_kind,
    select_runtime,
    tag,
)
from cairn.core.runtime.base import get_retry
from cairn.core.runtime.retry import (
    RetryPolicy,
    RetrySpec,
    default_for,
    resolve,
)
from cairn.core.runtime.temporal import is_available


# ─── tag / get_kind / get_retry ──────────────────────────────────────


def test_tag_stamps_kind_on_function():
    def node(state):
        return {}

    tag(node, NodeKind.LLM)
    assert get_kind(node) == NodeKind.LLM


def test_tag_stamps_kind_on_lambda():
    fn = tag(lambda s: {}, NodeKind.PURE)
    assert get_kind(fn) == NodeKind.PURE


def test_untagged_defaults_to_io():
    def node(state):
        return {}

    # IO is the safe default — round-trip through an activity rather than risk
    # a non-determinism error in the workflow sandbox.
    assert get_kind(node) == NodeKind.IO


def test_tag_with_retry_stamps_both():
    def node(state):
        return {}

    policy = RetryPolicy(maximum_attempts=7, start_to_close_timeout_seconds=42)
    tag(node, NodeKind.LLM, retry=policy)
    assert get_kind(node) == NodeKind.LLM
    assert get_retry(node) == policy


def test_tag_without_retry_returns_none():
    def node(state):
        return {}

    tag(node, NodeKind.TOOL)
    assert get_retry(node) is None


# ─── retry.resolve() ─────────────────────────────────────────────────


def test_default_for_each_kind_returns_distinct_policy():
    pure = default_for(NodeKind.PURE)
    llm = default_for(NodeKind.LLM)
    tool = default_for(NodeKind.TOOL)
    io = default_for(NodeKind.IO)
    # PURE inlines; only 1 attempt and small timeout
    assert pure.maximum_attempts == 1
    # LLM gets a generous timeout
    assert llm.start_to_close_timeout_seconds >= 60
    # TOOL gets more attempts (network blips)
    assert tool.maximum_attempts >= 3
    assert io.maximum_attempts >= 1


def test_resolve_with_no_override_returns_default():
    assert resolve(NodeKind.LLM, None) == default_for(NodeKind.LLM)


def test_resolve_merges_partial_override_over_default():
    base = default_for(NodeKind.LLM)
    override = RetrySpec(start_to_close_timeout_seconds=180.0)
    merged = resolve(NodeKind.LLM, override)
    # Overridden field changed
    assert merged.start_to_close_timeout_seconds == 180.0
    # Non-overridden field preserved from default
    assert merged.maximum_attempts == base.maximum_attempts
    assert merged.backoff_coefficient == base.backoff_coefficient


def test_resolve_full_override_replaces_every_field():
    override = RetrySpec(
        maximum_attempts=9,
        initial_interval_seconds=0.5,
        backoff_coefficient=3.0,
        maximum_interval_seconds=120.0,
        start_to_close_timeout_seconds=240.0,
    )
    merged = resolve(NodeKind.LLM, override)
    assert merged.maximum_attempts == 9
    assert merged.initial_interval_seconds == 0.5
    assert merged.backoff_coefficient == 3.0
    assert merged.maximum_interval_seconds == 120.0
    assert merged.start_to_close_timeout_seconds == 240.0


def test_retry_policy_to_temporal_returns_plugin_shape():
    pol = RetryPolicy(
        maximum_attempts=3,
        initial_interval_seconds=2.0,
        backoff_coefficient=2.0,
        maximum_interval_seconds=30.0,
        start_to_close_timeout_seconds=120.0,
    )
    out = pol.to_temporal()
    assert "start_to_close_timeout" in out
    assert "retry_policy_kwargs" in out
    kwargs = out["retry_policy_kwargs"]
    assert kwargs["maximum_attempts"] == 3
    assert kwargs["backoff_coefficient"] == 2.0


# ─── select_runtime ──────────────────────────────────────────────────


def test_select_runtime_default_is_inprocess():
    rt = select_runtime()
    assert isinstance(rt, InProcessRuntime)


def test_select_runtime_inprocess_explicit():
    rt = select_runtime("inprocess")
    assert isinstance(rt, InProcessRuntime)


def test_select_runtime_unknown_falls_back_to_inprocess():
    rt = select_runtime("does-not-exist")
    assert isinstance(rt, InProcessRuntime)


def test_select_runtime_temporal_requires_extras():
    if is_available():
        # The SDK is installed — selection should succeed.
        rt = select_runtime("temporal")
        assert rt.__class__.__name__ == "TemporalRuntime"
    else:
        # Not installed — selection should fail clean with a helpful message.
        with pytest.raises(ImportError, match="temporalio"):
            select_runtime("temporal")


# ─── compiler integration: action node retry resolution ──────────────


def test_action_node_def_accepts_retry_spec():
    """Frontmatter validates and survives round-trip through ActionNodeDef."""
    from cairn.core.engine.schema import ActionNodeDef

    nd = ActionNodeDef.model_validate({
        "type": "prompt",
        "model": "fast",
        "retry": {"start_to_close_timeout_seconds": 180.0, "maximum_attempts": 2},
    })
    assert nd.retry is not None
    assert nd.retry.start_to_close_timeout_seconds == 180.0
    assert nd.retry.maximum_attempts == 2


def test_action_node_def_retry_optional():
    """No retry block in frontmatter → retry is None; defaults apply at runtime."""
    from cairn.core.engine.schema import ActionNodeDef

    nd = ActionNodeDef.model_validate({"type": "prompt", "model": "fast"})
    assert nd.retry is None


# ─── temporal subpackage import-safety ───────────────────────────────


def test_temporal_subpackage_imports_without_sdk():
    """Importing the package itself must not require temporalio."""
    import cairn.core.runtime.temporal as mod  # noqa: F401

    # is_available is the only function safe to call without the SDK
    assert isinstance(is_available(), bool)
