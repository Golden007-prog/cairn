"""TriggerProvider abstract — the receiver side of the trigger seam.

The locked architecture splits trigger responsibility three ways:

  Domain pack    → ``triggers/<source>.yaml`` (intent + normalize mapping)
  Operator pack  → ``src/operator_<n>/triggers/<source>.py`` (receiver)
  Core router    → routes normalized events to template runs

This module owns the *receiver* contract: a :class:`TriggerProvider` verifies
and normalises incoming events for one ``source``. Providers are registered
in-process (:data:`default_provider_registry`); the generic YAML provider in
:mod:`cairn.core.triggers.router` serves every declared source, so no vendor
provider is required. The ``cairn.trigger_providers`` entry-point group was
retired (ADR-0120): no distribution ever provided it.

Phase 2.8 ships the abstract + the loader + the registry. It does NOT
ship any concrete provider — those land with per-source operator packs
(``operator-pagerduty``, ``operator-prometheus``, ``operator-github``,
etc.) in Phase 2.9+. The composition validation in this slice catches
"pack declares source X but no provider for X is installed" as a hard
fail so the gap is loud, not silent.

Provider responsibilities:

  1. ``verify(headers, body)``     — signature check; True/False
  2. ``normalize(headers, body)``  — return one normalized dict per event

Both methods stay sync because verification + normalization are pure
functions of the inbound payload (no network calls). If a future
provider needs async — e.g. a vendor that requires a callback to
resolve a payload — the contract extends to optional async methods
the runtime awaits if present, mirroring the
:class:`cairn.core.operator.Operation` async-run pattern.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Optional, Type

logger = logging.getLogger("cairn.core.triggers.provider")



class TriggerProvider(ABC):
    """Abstract receiver for one external source.

    Subclasses pin the ``source`` class attribute to match the
    ``source:`` field of a domain pack's trigger YAML — that's the
    routing key the substrate uses to dispatch incoming payloads.

    Subclasses MAY override:

      - ``signature_required`` — False when the upstream doesn't sign
        (Prometheus Alertmanager by default); True otherwise.

    Subclasses MUST implement:

      - :meth:`verify` — return True iff the signature checks out
        (or signature_required is False).
      - :meth:`normalize` — project the vendor payload into the
        normalized event dict the router expects.
    """

    #: Routing key. Subclasses set this to match the corresponding
    #: trigger YAML's ``source:`` field (e.g. ``"pagerduty"``).
    source: str = ""

    #: Whether the upstream signs payloads. False for senders that
    #: rely on network controls (e.g. Prometheus by default).
    signature_required: bool = True

    @abstractmethod
    def verify(
        self,
        headers: dict[str, str],
        body: bytes,
    ) -> bool:
        """Return True iff the request signature is valid (or unrequired)."""

    @abstractmethod
    def normalize(
        self,
        headers: dict[str, str],
        body: bytes,
    ) -> list[dict[str, Any]]:
        """Project the vendor payload into normalized event dicts.

        Returns **one dict per event**. A plain webhook yields a
        single-element list; a payload that batches events (Prometheus
        Alertmanager groups, GitHub push with many commits) applies the
        trigger YAML's ``fan_out`` spec and yields one dict per item.
        The router matches + dispatches one workflow run per returned
        event.
        """


# ─── registry ────────────────────────────────────────────────────────


class _ProviderRegistry:
    """Source → TriggerProvider-subclass lookup.

    Mirrors :class:`cairn.core.operator.registry.OperatorRegistry`
    shape: register / get / list_sources / clear. The router consumes
    via :meth:`get`; doctor via :meth:`list_sources`.
    """

    def __init__(self) -> None:
        self._providers: dict[str, Type[TriggerProvider]] = {}

    def register(
        self, provider_cls: Type[TriggerProvider], *, strict: bool = False,
    ) -> None:
        source = getattr(provider_cls, "source", None)
        if not source:
            raise ValueError(
                f"{provider_cls.__name__} is missing `source` — "
                "set the class attribute before registering."
            )
        existing = self._providers.get(source)
        if existing is provider_cls:
            return  # idempotent re-register
        if existing is not None and strict:
            raise ValueError(
                f"trigger provider for source {source!r} already "
                f"registered as {existing.__module__}.{existing.__name__}; "
                f"refusing to overwrite with "
                f"{provider_cls.__module__}.{provider_cls.__name__}"
            )
        if existing is not None:
            logger.warning(
                "trigger provider for source %r re-registered: %s.%s → %s.%s",
                source, existing.__module__, existing.__name__,
                provider_cls.__module__, provider_cls.__name__,
            )
        self._providers[source] = provider_cls

    def get(self, source: str) -> Optional[Type[TriggerProvider]]:
        return self._providers.get(source)

    def has(self, source: str) -> bool:
        return source in self._providers

    def list_sources(self) -> list[str]:
        return sorted(self._providers.keys())

    def all(self) -> dict[str, Type[TriggerProvider]]:
        return dict(self._providers)

    def clear(self) -> None:
        self._providers.clear()


default_provider_registry = _ProviderRegistry()


__all__ = [
    "TriggerProvider",
    "default_provider_registry",
]
