"""The ONE runtime PolicyEngine the dispatch chokepoint consults (#1095, ADR-0083 D3/R1).

Two problems this module closes:

* **P1 — the policy source never reached dispatch.** ``resolve_gate`` has always
  accepted a ``policy_engine`` (ADR-0034 source 3), but the runtime passed
  ``None`` ("wired later"), so policy was evaluated at run ADMISSION only. An
  environment-attached policy authored in the console (#316/#317) — "gate every
  ``external_write``" — was silently inert at the per-operation gate.
* **P2 — no shared evaluator.** ``PolicyEngine()`` was constructed ad hoc at 4+
  sites. Building one per operation re-reads every pack policy dir plus the
  console store on every dispatch, and lets two call sites disagree about the
  effective policy set.

The engine is built ONCE per process and cached. It is built exactly the way
:class:`cairn.core.engine.runner.Runner` builds its own — same pack/overlay/store
layering, same ``attached_store_policies`` attach-list from the ambient profile
(#317, ADR-0053) — so admission-time and dispatch-time enforcement can never
diverge into two different views of the same deployment.

Invalidate with :func:`reset_runtime_policy_engine` after a policy write (the
console's policy CRUD) or in tests.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger("cairn.approval.policy_source")

_lock = threading.Lock()
_engine: Optional[Any] = None
#: Set when construction failed, so we don't retry a broken build on every
#: dispatch (a policy store that is down would otherwise cost an attempt per
#: operator invocation). Cleared by ``reset_runtime_policy_engine``.
_build_failed = False


def _build_engine() -> Any:
    """Construct the PolicyEngine exactly as the Runner does.

    Imports are local: this module sits on the operator-dispatch path, and the
    policy package pulls in the pack loader — keeping it lazy avoids an import
    cycle (approval → policy → pack → operator) and keeps ``import cairn`` cheap.
    """
    from cairn.core.engine.runner import (  # noqa: PLC0415 - cycle: approval → engine.runner
        _adopted_pack_policies_from_env,
        _attached_store_policies_from_env,
    )
    from cairn.core.policy.engine import PolicyEngine  # noqa: PLC0415 - cycle: approval → policy → pack

    return PolicyEngine(
        attached_store_policies=_attached_store_policies_from_env(),
        # #1103 — the same pack-policy adoption list the admission plane uses, so
        # the two planes cannot disagree about which policies this deployment has.
        adopted_pack_policies=_adopted_pack_policies_from_env(),
    )


def runtime_policy_engine() -> Optional[Any]:
    """The shared PolicyEngine, or ``None`` when it cannot be constructed.

    ``None`` means "this deployment has no usable policy plane" — the caller
    then falls back to the other gate authorities (operator baseline, profile
    switch, binding). It does NOT mean "allow": a policy engine that builds but
    *fails to evaluate* gates fail-closed inside ``resolve_gate``.
    """
    global _engine, _build_failed
    if _engine is not None:
        return _engine
    if _build_failed:
        return None
    with _lock:
        if _engine is not None:
            return _engine
        if _build_failed:
            return None
        try:
            _engine = _build_engine()
        except Exception as e:  # noqa: BLE001 — a missing policy plane must not break dispatch
            _build_failed = True
            logger.warning(
                "runtime policy engine unavailable (%s) — the per-operation gate "
                "falls back to operator/profile/binding authorities", e,
            )
            return None
        logger.debug("runtime policy engine built: %d policies", len(getattr(_engine, "policies", [])))
        return _engine


def reset_runtime_policy_engine() -> None:
    """Drop the cached engine so the next call rebuilds it.

    Call after any policy write (console CRUD, ``cairn`` policy commands) so a
    newly attached policy takes effect without a process restart.
    """
    global _engine, _build_failed
    with _lock:
        _engine = None
        _build_failed = False


__all__ = ["runtime_policy_engine", "reset_runtime_policy_engine"]
