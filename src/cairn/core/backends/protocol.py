"""The backend contract + capability validation — domain-neutral.

This is the engine-side definition that ``core/registry.py`` validates against.
It deliberately knows nothing about observability (or any other domain): a
backend is just an object that can answer two questions about capability names.

A pack supplies the concrete backend (e.g. the incident pack's ``ObsBackend``
ABC with ``search_logs`` / ``search_traces`` / … and its capability→method
map). Those structurally satisfy :class:`BackendProtocol` without importing it.
"""

from __future__ import annotations

from typing import Collection, Protocol, runtime_checkable


class CapabilityNotSupportedByBackend(RuntimeError):
    """Raised when a template requests a capability the active backend can't serve."""


@runtime_checkable
class BackendProtocol(Protocol):
    """Structural contract for a data backend.

    ``supported_capabilities()`` — capability names this *instance* actually
    serves. ``managed_capabilities()`` — the universe of capability names a
    backend of this *kind* is responsible for (a superset of, or equal to, the
    supported set). The validator only flags a requested capability when it is
    *managed* by the backend but not *supported* by this instance; capabilities
    outside the managed universe (e.g. document retrieval handled elsewhere)
    are simply not the backend's concern.
    """

    def supported_capabilities(self) -> Collection[str]: ...

    def managed_capabilities(self) -> Collection[str]: ...


def validate_capabilities_against_backend(
    capabilities: list[str],
    backend: BackendProtocol,
) -> None:
    """Compile-time check: every *backend-managed* capability must be served.

    Capabilities outside ``backend.managed_capabilities()`` are skipped — they
    get validated elsewhere (e.g. against the universe in
    ``CapabilityRegistry.validate``).
    """
    supported = set(backend.supported_capabilities())
    managed = set(backend.managed_capabilities())
    unsupported = [c for c in capabilities if c in managed and c not in supported]
    if unsupported:
        raise CapabilityNotSupportedByBackend(
            f"Active backend {type(backend).__name__} does not support "
            f"capabilities: {unsupported}. Backend supports: "
            f"{sorted(supported)}"
        )


__all__ = [
    "BackendProtocol",
    "CapabilityNotSupportedByBackend",
    "validate_capabilities_against_backend",
]
