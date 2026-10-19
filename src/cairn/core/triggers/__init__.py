"""Trigger ingress + routing.

Three sub-modules implement the locked trigger seam:

  - :mod:`cairn.core.engine.triggers_spec`     — parsed shape of a domain
    pack's ``triggers/<source>.yaml`` (intent + normalize mapping).
  - :mod:`cairn.core.triggers.provider` — abstract receiver
    contract that operator packs implement (verify + normalize). Entry-
    providers are registered in-process; the generic YAML provider serves
    every declared source (the ``cairn.trigger_providers`` entry-point group
    was retired, ADR-0120).
  - :mod:`cairn.core.triggers.router`   — generic YAML-driven provider
    + :class:`TriggerRouter` (verify → normalize → match → run a template).
  - :mod:`cairn.core.triggers.server`   — FastAPI ingress; mounts the
    router's spec-declared routes (§24.2) alongside the legacy endpoints.

This package re-exports the public surface for callers.
"""

from cairn.core.triggers.provider import (
    TriggerProvider,
    default_provider_registry,
)
from cairn.core.triggers.router import (
    GenericYamlProvider,
    RouteResult,
    TriggerRouter,
)
from cairn.core.engine.triggers_spec import (
    FanOutSpec,
    IngressSpec,
    RunSpec,
    SignatureSpec,
    TriggerSpec,
    TriggerSpecError,
    load_trigger_spec,
    load_trigger_specs,
)

__all__ = [
    "TriggerSpec",
    "IngressSpec",
    "SignatureSpec",
    "FanOutSpec",
    "RunSpec",
    "TriggerSpecError",
    "load_trigger_spec",
    "load_trigger_specs",
    "TriggerProvider",
    "default_provider_registry",
    "TriggerRouter",
    "GenericYamlProvider",
    "RouteResult",
]
