"""Runtime enforcement of the approval gate at the operation-invocation
chokepoint (ADR-0034, P1).

`invoke_operator` calls `resolve_runtime_gate(...)` before dispatch; if a gate is
required and `state` carries no approval for this node, it raises
`NeedsApprovalError`. The compiled operation-node fn (P2) catches that, pauses
via LangGraph `interrupt()`, and re-invokes with the `ApprovalRecord` recorded in
state — so the chokepoint then lets it through.

Gate sources (ADR-0034, all four now live): operator-author `requires_approval`
(pack.toml metadata, not pack-controllable) + the `[profile.approval]` deployment
coarse-switch + the pack binding (strictness-only) + the cross-cutting
**PolicyEngine** — wired in #1095 (ADR-0083 D3/R1) via
:mod:`cairn.core.approval.policy_source`. Until then this passed
`policy_engine=None`, so an environment-attached policy ("gate every
`external_write`") was evaluated at run admission but silently inert at the
per-operation chokepoint.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from cairn.core.approval.resolver import ApprovalGate, resolve_gate

logger = logging.getLogger("cairn.approval.enforce")

#: state[APPROVALS_STATE_KEY][node_name] = ApprovalRecord dict (set by the node fn)
APPROVALS_STATE_KEY = "_approvals"


@dataclass(frozen=True)
class ApprovalRequest:
    """The payload surfaced when a gate opens (via `interrupt()` / the bus).

    Carries no secret — just what an approver needs to decide.
    """
    operation_id: str
    run_id: str
    operator: str
    sources: tuple[str, ...]
    risk: Optional[str] = None
    capability: Optional[str] = None
    reason: Optional[str] = None
    channel: Optional[str] = None
    timeout_seconds: Optional[int] = None
    allowed_decisions: tuple[str, ...] = ("approve", "reject")


class NeedsApprovalError(Exception):
    """Raised at the dispatch chokepoint when a gated op has no approval yet."""
    def __init__(self, request: ApprovalRequest):
        self.request = request
        super().__init__(
            f"operation {request.operator!r} requires human approval "
            f"(sources: {', '.join(request.sources) or 'unknown'})"
        )


def get_approval(state: dict, node_name: str) -> Optional[dict]:
    """The ApprovalRecord dict recorded for this node, or None."""
    return (state.get(APPROVALS_STATE_KEY) or {}).get(node_name)


def _profile_approval() -> Optional[dict]:
    try:
        from cairn.core.pack.profile import load_profile_from_env
        return getattr(load_profile_from_env(), "approval", None)
    except Exception as e:  # noqa: BLE001 — no profile = no coarse switch
        logger.debug("no profile approval config: %s", e)
        return None


def _runtime_policy_engine() -> Optional[Any]:
    """The shared PolicyEngine for dispatch-time gating (#1095).

    Indirection kept for two reasons: tests substitute it, and a deployment with
    no usable policy plane degrades to the other authorities instead of raising
    on every operator invocation.
    """
    from cairn.core.approval.policy_source import (  # noqa: PLC0415 - cycle: approval → policy → pack
        runtime_policy_engine,
    )
    return runtime_policy_engine()


def resolve_runtime_gate(operator_cls: Any, binding: Any, *, state: dict) -> ApprovalGate:
    """Resolve the effective gate for an operation at dispatch time.

    All four ADR-0034 authorities are consulted: operator baseline, deployment
    profile switch, deployment **policy** (#1095), and the pack binding
    (strictness-only). Merge is strictness-only — any authority can mandate a
    gate; the binding can never relax one.
    """
    name = getattr(operator_cls, "name", getattr(operator_cls, "__name__", "?"))
    from cairn.core.operator.registry import default_registry
    md = default_registry.get_metadata(name)
    try:
        policy_engine = _runtime_policy_engine()
    except Exception as e:  # noqa: BLE001 — an unavailable policy plane must not break dispatch
        logger.warning("policy source unavailable at dispatch (%s)", e)
        policy_engine = None
    return resolve_gate(
        operator_name=name,
        risk=getattr(md, "risk", None) if md else None,
        requires_approval=bool(getattr(md, "requires_approval", False)) if md else False,
        capabilities=list(getattr(md, "provides_capabilities", []) or []) if md else [],
        environment=str(state.get("environment") or "production"),
        policy_engine=policy_engine,
        binding_approval=getattr(binding, "approval", None),
        profile_approval=_profile_approval(),
    )


def enforce_gate(
    operator_cls: Any,
    binding: Any,
    *,
    state: dict,
    node_name: str,
    run_id: str,
    is_mocked: bool,
) -> None:
    """THE enforcement point: raise if a required gate has no recorded approval.

    Every path that dispatches an operator calls this — the compiled template
    node via :func:`~cairn.core.operator.runtime.invoke_operator`, and the
    pipeline bridge's ``task:`` legs. It exists as one function precisely
    because it used to be one *inline block*: #1278 added a second dispatch
    path that reproduced the binding-layer steps but not this one, so an
    operator declaring ``requires_approval`` ran ungated as a task leg
    (#1402). A second call path that reimplements part of the first is how
    enforcement silently diverges (#1048) — so callers get this function, not
    a recipe to copy.

    ``binding`` is consulted for ``.approval`` only, and strictness-only: a
    binding (or a pipeline step) can mandate a gate, never relax one. Callers
    without a real binding pass any object exposing ``.approval`` — see
    :class:`ApprovalIntent`.

    Only FULL mock skips the check; a Playground *sandbox* run enforces HITL
    for real (ADR-0073), pausing before a mock-stubbed write.
    """
    from cairn.core.operator.runtime import sandbox_mode_active  # noqa: PLC0415 - cycle: operator → approval

    if is_mocked and not sandbox_mode_active():
        return
    gate = resolve_runtime_gate(operator_cls, binding, state=state)
    if gate.required and get_approval(state, node_name) is None:
        raise NeedsApprovalError(
            gate_to_request(
                gate,
                operator=getattr(operator_cls, "name", node_name),
                node_name=node_name,
                run_id=str(run_id),
            )
        )


@dataclass(frozen=True)
class ApprovalIntent:
    """The ``.approval`` carrier for callers that have no pack binding.

    A pipeline ``task:`` leg resolves its inputs before dispatch, so there is
    no :class:`OperationBinding` to consult — but the step may still declare
    ``approval: true``. This adapts that declaration to the shape
    :func:`enforce_gate` reads, without pretending to be a binding.
    """
    approval: Optional[Any] = None


def enforce_flow_gate(
    ref: str, risk: Optional[str], binding_approval: Any, *,
    state: dict, node_name: str, run_id: str,
) -> None:
    """THE enforcement point for a leg that has no operator class (#1760).

    Flow legs reached the engine without ever consulting the approval plane:
    :func:`enforce_gate` is called from the compiled template node and from the
    bridge's ``task:`` legs, and from nowhere else. So a deployment policy
    naming a risk class governed ``task:`` legs and silently ignored ``flow:``
    legs of the SAME class — ``eval.closedbook`` is ``external_write`` (paid
    judge calls + endpoint traffic) and sailed through while ``dataset/replay``
    was gated. That is the #1688 / #1402 shape a third time: the enforcement
    point exists, and a whole dispatch path does not call it.

    It lives HERE, beside :func:`enforce_gate`, rather than in the bridge, for
    the reason #1402 exists: a second call path that reproduces the authority
    merge is how enforcement silently diverges. Both raise the same
    :class:`NeedsApprovalError`, so one caller-side catch covers both kinds.

    ``risk`` is the flow's ``aggregate_risk`` over its stage kernels — the same
    number the admission plane computes and the builder displays. ``None`` (an
    unreadable flow def) resolves through the shared table like any unknown
    risk; it is never treated as "no risk".

    A flow def declares no operator baseline and no capabilities, so those two
    authorities are empty by construction; profile, policy and the step's own
    ``approval:`` still apply exactly as they do for an operator.
    """
    if get_approval(state, node_name) is not None:
        return
    try:
        policy_engine = _runtime_policy_engine()
    except Exception as e:  # noqa: BLE001 — an unavailable policy plane must not break dispatch
        logger.warning("policy source unavailable at flow dispatch (%s)", e)
        policy_engine = None
    gate = resolve_gate(
        operator_name=ref,
        risk=risk,
        requires_approval=False,
        capabilities=[],
        environment=str(state.get("environment") or "production"),
        policy_engine=policy_engine,
        binding_approval=binding_approval,
        profile_approval=_profile_approval(),
    )
    if gate.required:
        raise NeedsApprovalError(
            gate_to_request(gate, operator=ref, node_name=node_name, run_id=str(run_id))
        )


def approved_state(environment: str, node_name: str, approver: Optional[str]) -> dict:
    """The minimal gate-check state for a binding-free dispatch path.

    :func:`resolve_runtime_gate` reads only ``environment`` from state, and
    :func:`get_approval` reads the recorded approval. A pipeline step that a
    human released through the bridge's HITL queue carries its approver, and
    that IS the approval record — so the same human decision satisfies the
    operator's declared gate. No second approval mechanism.
    """
    state: dict[str, Any] = {"environment": environment or "production"}
    if approver:
        state[APPROVALS_STATE_KEY] = {
            node_name: {"decision": "approve", "approver": approver},
        }
    return state


def gate_to_request(
    gate: ApprovalGate, *, operator: str, node_name: str, run_id: str
) -> ApprovalRequest:
    return ApprovalRequest(
        operation_id=node_name,
        run_id=run_id,
        operator=operator,
        sources=gate.sources,
        risk=gate.risk,
        capability=gate.capability,
        reason=gate.reason,
        channel=gate.channel,
        timeout_seconds=gate.timeout_seconds,
        allowed_decisions=gate.allowed_decisions,
    )


# ── who may decide a pipeline gate (#2393) ───────────────────────────────────

_APPROVER_SEP = re.compile(r"[,\s]+")


def approver_names(required: str) -> list[str]:
    """The names a gate's ``approvers:`` value lists (comma/whitespace separated,
    order kept, blanks dropped)."""
    return [n for n in _APPROVER_SEP.split(str(required or "").strip()) if n]


def approver_allowed(required: str, *, username: str, role: str, is_admin: bool) -> bool:
    """May this principal decide a gate whose ``approvers:`` is ``required``?

    PURE, and the whole definition of what an ``approvers:`` value names (owner
    decision 2026-10-15, #2393): a **username** or a **role** (``owner`` /
    ``admin`` / ``member``). An owner/admin may always decide. A name that is
    neither — every shipped pack's ``ml-leads`` / ``data-stewards`` /
    ``platform`` / ``sre-oncall``, since no group concept exists — matches
    nobody, so such a gate is decidable by owner/admin only: fail CLOSED, never
    open. An empty value is an unstaffed gate and keeps the pre-#2393
    behaviour (any signed-in member). Auth-off has no principal and never
    reaches here.
    """
    if is_admin:
        return True
    names = approver_names(required)
    if not names:
        return True
    return (username or "") in names or (role or "") in names


__all__ = [
    "APPROVALS_STATE_KEY",
    "approver_allowed",
    "approver_names",
    "ApprovalIntent",
    "ApprovalRequest",
    "NeedsApprovalError",
    "approved_state",
    "enforce_flow_gate",
    "enforce_gate",
    "get_approval",
    "resolve_runtime_gate",
    "gate_to_request",
]
