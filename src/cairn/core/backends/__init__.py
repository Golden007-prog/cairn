"""Domain-neutral backend contract.

A *backend* is any data source a pack's tools read from. The engine knows
nothing about what a given backend serves (logs, compliance records, asset
inventories, …); it only knows the **contract**:

  - a backend declares which capability names it serves
    (``supported_capabilities()``),
  - and which capability names it is *responsible for routing*
    (``managed_capabilities()`` — the universe a backend of this kind covers,
    so the compiler can tell "you asked for a cap this backend type owns but
    this instance doesn't serve" apart from "this cap isn't a backend cap").

Packs implement this protocol with their own concrete backends (the incident
pack's ``ObsBackend`` and its clickstack / otel / mock adapters). Core never
imports those — it depends only on the structural protocol here.
"""

from cairn.core.backends.protocol import (
    BackendProtocol,
    CapabilityNotSupportedByBackend,
    validate_capabilities_against_backend,
)

__all__ = [
    "BackendProtocol",
    "CapabilityNotSupportedByBackend",
    "validate_capabilities_against_backend",
]
