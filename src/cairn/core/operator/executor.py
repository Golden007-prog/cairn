"""Executor dispatcher — picks how an operator runs.

Per ``architecture_obs_agent_locked.md`` §"Action lifecycle", the seven
locked executors are:

    in_process | mcp_tool | docker | kubernetes_job | temporal_activity
                | remote_job | http_call

Each operator manifest declares its supported subset via
``[operator."<n>"].executors``. At runtime this module picks one from
that subset and dispatches to the corresponding implementation. For
Phase 2.7, only ``in_process`` is implemented; the other six raise a
clean :class:`UnsupportedExecutorError` with a message naming the
phase that lands them. Adding a new executor is two steps:

  1. Add the implementation to :func:`invoke_operator` in ``runtime.py``.
  2. Add the executor name to :data:`_IMPLEMENTED_EXECUTORS`.

Selection logic, deliberately conservative:

  1. If ``policy.prefer`` is set AND the operator declares support AND
     we implement the preferred executor → pick it.
  2. Otherwise: walk the operator's declared executors in order and
     pick the first one we implement.
  3. If neither yields a match → :class:`UnsupportedExecutorError`.

An operator with no declared executors (no manifest metadata attached,
e.g. unit tests that bypass the loader) gets the default ``["in_process"]``
list so the runtime works in test fixtures without a pack.toml on disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Type

from cairn.core.operator.protocol import Operation
from cairn.core.operator.registry import (
    OperatorRegistry,
    default_registry,
)


class UnsupportedExecutorError(RuntimeError):
    """No executor in the operator's supported set is implemented yet.

    Carries the operator name, the declared executor list, and the
    implemented-set for diagnostics — the message tells the env owner
    which executor would need to land for the operator to dispatch.
    """


# Implemented executor set. ``in_process`` (P2.7) + ``mcp_tool`` (§24.3,
# dispatches a manifest-declared MCP tool through the substrate MCP
# client). The remaining locked-enum executors stub out with a clean
# UnsupportedExecutorError. Each new executor adds its name here when its
# dispatch branch lands in ``runtime.invoke_operator``.
_IMPLEMENTED_EXECUTORS: frozenset[str] = frozenset(
    {"in_process", "mcp_tool", "docker", "remote_job"}
)


# Phase-mapping for each unimplemented executor — surfaces in error
# messages so the user knows when to expect support. Updated as
# executors land. Not the source of truth (the roadmap is) — just an
# author-facing hint.
_PHASE_HINTS: dict[str, str] = {
    "kubernetes_job":    "Phase 4+ (k8s Job resource)",
    "temporal_activity": "Phase 4+ (durable runtime)",
    "http_call":         "Phase 5+ (plain HTTP to a long-lived service)",
}


@dataclass(frozen=True)
class ExecutorPolicy:
    """Per-run executor preference, typically built from env config.

    Phase 2.7 honors only ``prefer`` — when set and supported by the
    operator and implemented by the substrate, that executor is picked.
    Phase 3+ extensions include risk-based hard requirements (e.g.
    "production_mutation MUST use temporal_activity") and per-tier
    fallback chains.
    """

    #: Executor the env would prefer the operator dispatch through.
    #: ``None`` = no preference; picker uses the operator's first
    #: implemented executor.
    prefer: Optional[str] = None


def declared_executors(
    operator_cls: Type[Operation],
    *,
    registry: Optional[OperatorRegistry] = None,
) -> list[str]:
    """Return the operator's declared executor list.

    Looks up metadata via the registry. Returns ``["in_process"]`` when
    no metadata is attached (test fixtures, packs without a discovered
    pack.toml) so the runtime stays usable without forcing every test
    to wire a manifest.
    """
    target = registry if registry is not None else default_registry
    name = getattr(operator_cls, "name", None)
    if not name:
        return ["in_process"]
    md = target.get_metadata(name)
    if md is None or not md.executors:
        return ["in_process"]
    return list(md.executors)


def pick_executor(
    operator_cls: Type[Operation],
    *,
    policy: Optional[ExecutorPolicy] = None,
    registry: Optional[OperatorRegistry] = None,
) -> str:
    """Pick the executor to dispatch ``operator_cls`` through.

    See module docstring for the full selection logic. Returns the
    executor name (which the runtime then maps to its implementation).
    Raises :class:`UnsupportedExecutorError` only when the operator's
    declared list contains zero implemented executors — a deployment
    error the env owner needs to resolve before runs can proceed.
    """
    declared = declared_executors(operator_cls, registry=registry)
    name = getattr(operator_cls, "name", "<unknown>")

    # Step 1: honour an env-set preference when supported + implemented.
    if policy is not None and policy.prefer:
        if policy.prefer in declared and policy.prefer in _IMPLEMENTED_EXECUTORS:
            return policy.prefer

    # Step 2: first implemented from the declared list.
    for ex in declared:
        if ex in _IMPLEMENTED_EXECUTORS:
            return ex

    # Step 3: nothing matched — surface every unsupported executor with
    # its phase hint so the operator pack author knows when each lands.
    pending = [
        f"  {ex}: {_PHASE_HINTS.get(ex, 'no implementation yet')}"
        for ex in declared
    ]
    raise UnsupportedExecutorError(
        f"operator {name!r} declares executors {declared!r} but none are "
        f"implemented in this substrate version. Pending:\n"
        + "\n".join(pending)
    )


def is_implemented(executor: str) -> bool:
    """True if ``executor`` is in the implemented set.

    Doctor surfaces this — an operator declaring an unimplemented
    executor as its only option needs the env owner to know that
    `agentops run` would error against it.
    """
    return executor in _IMPLEMENTED_EXECUTORS


def implemented_executors() -> frozenset[str]:
    """Return the set of executor names currently implemented."""
    return _IMPLEMENTED_EXECUTORS


__all__ = [
    "ExecutorPolicy",
    "UnsupportedExecutorError",
    "declared_executors",
    "pick_executor",
    "is_implemented",
    "implemented_executors",
]
