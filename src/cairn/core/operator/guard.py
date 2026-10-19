"""Capability guard for lifted ``@step`` ops (#1254, ADR-0087 P2/D5).

An op lifted from a pipeline-class ``def`` declares its ctx capabilities in
``requires=[...]`` — fail-closed like every op (ADR-0053). The build lints the
obvious static case (AST finds ``ctx.X`` with no declaration), but linting
ASSISTS declaration, it never replaces it: dynamic access slips past any lint,
so the declared list is enforced again here, at runtime, on every attribute
read. Undeclared → :class:`CapabilityNotDeclared`, loudly, before the op can
touch a service it never told the audit trail about.

Ambient surface (identity + plumbing every op may use without declaring):
``pipeline_id`` / ``node_name`` / ``is_mocked`` / ``logger`` /
``emit_heartbeat`` / ``check_cancelled``. Everything else on
:class:`~cairn.core.operator.protocol.OperationContext` — ``artifacts``,
``secrets``, ``metrics``, ``cost``, ``compute``, and whatever lands later —
is a granted capability.
"""
from __future__ import annotations

from typing import Any, Iterable

#: Always-available context surface — identity and lifecycle plumbing, not
#: capabilities. Kept deliberately small: anything that reaches a SERVICE
#: (storage, secrets, network, money) must be declared.
AMBIENT_CONTEXT_ATTRS = frozenset({
    "pipeline_id", "node_name", "is_mocked",
    "logger", "emit_heartbeat", "check_cancelled",
})


class CapabilityNotDeclared(RuntimeError):
    """The op touched a ctx capability missing from its ``requires`` list."""


class GuardedContext:
    """A view over an :class:`OperationContext` limited to declared capabilities.

    A plain attribute proxy (not a subclass) so the guard composes with ANY
    context implementation the runtime hands out — Null* defaults, bound
    backends, or a test fake.
    """

    __slots__ = ("_ctx", "_allowed")

    def __init__(self, ctx: Any, requires: Iterable[str]) -> None:
        object.__setattr__(self, "_ctx", ctx)
        object.__setattr__(self, "_allowed",
                           frozenset(requires) | AMBIENT_CONTEXT_ATTRS)

    def __getattr__(self, name: str) -> Any:
        if name not in self._allowed:
            raise CapabilityNotDeclared(
                f"ctx.{name} is not in this step's requires list "
                f"(declared: {sorted(self._allowed - AMBIENT_CONTEXT_ATTRS)}) — "
                "declare it in @step(requires=[...]) and rebuild (ADR-0087 D5)."
            )
        return getattr(self._ctx, name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise CapabilityNotDeclared(
            f"lifted steps cannot set ctx.{name} — the context is a granted "
            "surface, not scratch state."
        )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<GuardedContext requires={sorted(self._allowed - AMBIENT_CONTEXT_ATTRS)}>"


def guard_context(ctx: Any, requires: Iterable[str]) -> GuardedContext:
    """Wrap ``ctx`` so only ``requires`` + ambient attributes are reachable."""
    return GuardedContext(ctx, requires)


__all__ = ["AMBIENT_CONTEXT_ATTRS", "CapabilityNotDeclared", "GuardedContext",
           "guard_context"]
