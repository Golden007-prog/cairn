"""Retry policy resolution — per-NodeKind defaults with per-template override.

Two layers, in priority order (later wins):

  1. **Per-kind defaults** (this module's :data:`_DEFAULTS`). What every node of
     a given :class:`NodeKind` gets out of the box. LLM nodes get a longer
     timeout than IO; PURE nodes get no retry (they're inline).
  2. **Per-template override** — a ``retry:`` block on an ``ActionNodeDef`` in
     the template frontmatter. Lets a template tune one specific node (e.g.
     "this LLM call is slow, give it 90s") without changing the runtime
     defaults for every other LLM node.

Pure-Python; zero ``temporalio`` import. The Temporal runtime translates a
:class:`RetryPolicy` into ``temporalio.common.RetryPolicy`` at worker boot.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Optional

from cairn.core.runtime.base import NodeKind


@dataclass(frozen=True)
class RetryPolicy:
    """Retry + timeout policy for a single node under a durable runtime.

    Fields mirror ``temporalio.common.RetryPolicy`` plus the activity timeout
    (which Temporal expects alongside the policy, not on it). Keeping them
    together here so a template author has one block to think about.
    """

    maximum_attempts: int = 3
    initial_interval_seconds: float = 1.0
    backoff_coefficient: float = 2.0
    maximum_interval_seconds: float = 60.0
    start_to_close_timeout_seconds: float = 60.0
    # ADR-0080 P0 (#1028) — heartbeat timeout. When set, Temporal fails the
    # activity if it doesn't heartbeat within this window (fast stuck-detection
    # instead of waiting out the full start_to_close). Only meaningful for an
    # operator that actually emits heartbeats; ``None`` = no heartbeat.
    heartbeat_timeout_seconds: Optional[float] = None

    def to_temporal(self) -> dict[str, Any]:
        """Translate to the kwargs the Temporal LangGraph plugin expects.

        Returned as a dict so this module stays import-free of ``temporalio``;
        the temporal runtime builds the real ``RetryPolicy`` from it. The
        ``heartbeat_timeout`` key is present only when a heartbeat is declared —
        setting it on a non-heartbeating activity would fail it spuriously.
        """
        out: dict[str, Any] = {
            "start_to_close_timeout": timedelta(seconds=self.start_to_close_timeout_seconds),
            "retry_policy_kwargs": {
                "maximum_attempts": self.maximum_attempts,
                "initial_interval": timedelta(seconds=self.initial_interval_seconds),
                "backoff_coefficient": self.backoff_coefficient,
                "maximum_interval": timedelta(seconds=self.maximum_interval_seconds),
            },
        }
        if self.heartbeat_timeout_seconds is not None:
            out["heartbeat_timeout"] = timedelta(seconds=self.heartbeat_timeout_seconds)
        return out


# Per-kind defaults. Tuned for what each kind actually does:
#   - PURE: inline in workflow → no retry/timeout fields apply, but we still
#           materialize a policy for uniformity.
#   - LLM:  slow + occasionally flaky → longer timeout, moderate retries.
#   - TOOL: MCP/backend HTTP → faster timeout, more retries (network blips).
#   - IO:   user Python or a nested template → conservative timeout, one retry.
_DEFAULTS: dict[NodeKind, RetryPolicy] = {
    NodeKind.PURE: RetryPolicy(
        maximum_attempts=1,
        start_to_close_timeout_seconds=10.0,
    ),
    NodeKind.LLM: RetryPolicy(
        maximum_attempts=3,
        initial_interval_seconds=2.0,
        backoff_coefficient=2.0,
        maximum_interval_seconds=30.0,
        start_to_close_timeout_seconds=120.0,
    ),
    NodeKind.TOOL: RetryPolicy(
        maximum_attempts=5,
        initial_interval_seconds=1.0,
        backoff_coefficient=2.0,
        maximum_interval_seconds=15.0,
        start_to_close_timeout_seconds=60.0,
    ),
    NodeKind.IO: RetryPolicy(
        maximum_attempts=2,
        initial_interval_seconds=1.0,
        backoff_coefficient=2.0,
        maximum_interval_seconds=10.0,
        # IO covers a nested template or ordinary user Python — seconds-to-minutes.
        # 600s is generous for that. Heavy dataset operators (anonymize / pii_scan)
        # NO LONGER rely on this blanket: ADR-0080 P0 (#1028) lets an operator
        # DECLARE `activity_timeout_seconds`, so a full-corpus scan asks for what it
        # needs (e.g. 3600s) and a quick probe stays snappy. This retires the
        # blanket-1800 crutch (#1023, raised as a symptom patch when the whole
        # sharded anonymize was ONE IO activity with no per-operator budget).
        start_to_close_timeout_seconds=600.0,
    ),
}


def default_for(kind: NodeKind) -> RetryPolicy:
    """Return the baseline policy for ``kind``."""
    return _DEFAULTS[kind]


def resolve(
    kind: NodeKind,
    override: Optional["RetrySpec"] = None,
) -> RetryPolicy:
    """Merge a template-level :class:`RetrySpec` over the per-kind default.

    Fields the override leaves as ``None`` keep the default's value. The
    override never widens a field implicitly — a template that wants a longer
    timeout has to set ``start_to_close_timeout_seconds`` itself.
    """
    base = default_for(kind)
    if override is None:
        return base
    return replace(
        base,
        **{k: v for k, v in override.as_dict().items() if v is not None},
    )


# A lightweight schema-side type. ``ActionNodeDef.retry`` is a Pydantic model
# with the same shape; this dataclass is the engine-side view.
@dataclass(frozen=True)
class RetrySpec:
    """A per-template override. Any ``None`` field falls through to the default."""

    maximum_attempts: Optional[int] = None
    initial_interval_seconds: Optional[float] = None
    backoff_coefficient: Optional[float] = None
    maximum_interval_seconds: Optional[float] = None
    start_to_close_timeout_seconds: Optional[float] = None
    heartbeat_timeout_seconds: Optional[float] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "maximum_attempts": self.maximum_attempts,
            "initial_interval_seconds": self.initial_interval_seconds,
            "backoff_coefficient": self.backoff_coefficient,
            "maximum_interval_seconds": self.maximum_interval_seconds,
            "start_to_close_timeout_seconds": self.start_to_close_timeout_seconds,
            "heartbeat_timeout_seconds": self.heartbeat_timeout_seconds,
        }
