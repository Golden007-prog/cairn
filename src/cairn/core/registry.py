"""
Capability registry: resolves capability names to concrete tools.

Two layers of validation happen at template-compile time:

  1. **Universe check** — is this a real capability name the platform knows?
     Resolves against ``tool_adapter.list_capabilities()`` (the canonical
     registry of capability names).

  2. **Backend support check** — does the currently-active backend actually
     serve this capability? Resolves against
     ``backend.supported_capabilities()``.

Failing fast at compile time means bad templates don't blow up mid-investigation.
"""

from __future__ import annotations

from typing import Iterable

from langchain_core.tools import StructuredTool

from cairn.core.backends import (
    BackendProtocol,
    CapabilityNotSupportedByBackend,
    validate_capabilities_against_backend,
)
from cairn.core.clients.mcp_client import tool_adapter


class UnknownCapabilityError(KeyError):
    """Raised when a template requests a capability not registered with any backend."""


class CapabilityRegistry:
    """Resolves capability names to LangChain-callable tools.

    By default validates only against the universe of known capabilities.
    Construct with ``backend=`` to also validate against what the active
    backend actually serves.
    """

    def __init__(self, backend: BackendProtocol | None = None) -> None:
        # Universe of capability names the platform knows about.
        self._known: set[str] = set(tool_adapter.list_capabilities())
        # Optional: validate against an actual backend's supported set.
        self._backend = backend

    def validate(self, capabilities: Iterable[str]) -> None:
        """Raise on unknown capabilities OR on capabilities the backend can't serve.

        Call this at compile time so bad templates fail fast with a clear error
        rather than at runtime.
        """
        capability_list = list(capabilities)

        # Layer 1: universe check
        missing = [c for c in capability_list if c not in self._known]
        if missing:
            known = ", ".join(sorted(self._known))
            raise UnknownCapabilityError(
                f"Unknown capabilities: {missing}. Known capabilities: {known}"
            )

        # Layer 2: backend support check (only if a backend was supplied)
        if self._backend is not None:
            validate_capabilities_against_backend(capability_list, self._backend)

    def resolve(self, capabilities: Iterable[str]) -> list[StructuredTool]:
        """Return LangChain tools for the given capabilities.

        Deduplicates while preserving input order. Raises on unknown capabilities
        or on capabilities the active backend doesn't support.
        """
        capability_list = list(capabilities)
        self.validate(capability_list)
        return tool_adapter.make_tools_for_capabilities(capability_list)


# Module-level singleton for convenience. Does NOT check backend support —
# tests and demos that want strict backend validation should construct their
# own ``CapabilityRegistry(backend=...)``.
default_registry = CapabilityRegistry()


# Re-export for convenience so callers only import from one module
__all__ = [
    "CapabilityNotSupportedByBackend",
    "CapabilityRegistry",
    "UnknownCapabilityError",
    "default_registry",
]
