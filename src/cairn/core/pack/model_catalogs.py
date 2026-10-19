"""Model-catalogue providers, discovered through the ``cairn.model_catalogs``
entry-point group (ADR-0120 D4, #2414).

The licence door (:mod:`cairn.core.model_license_gate`) asks whether a run's
``base_model`` names a *gated* catalogue entry. WHICH catalogue exists belongs
to whoever ships the models, so the core names none: every provider is an
entry point.

    [project.entry-points."cairn.model_catalogs"]
    training = "cairn_platform.models_catalog:get_catalog_entry"

A provider is a callable ``(model_id: str) -> entry | None``. An entry exposes
``gated`` (bool) and ``license`` (str or None); the shipped one is
:class:`cairn_platform.models_catalog.ModelCatalogEntry`. :func:`lookup` asks the
providers in entry-point name order and returns the first entry found.

Absent group = no catalogue: ``lookup`` returns ``None`` and a ``base_model``
input is not checked against one (ADR-0120 D4, "absent means"). Discovery fails
open like :mod:`cairn.core.pack.loader` and :mod:`cairn.core.pack.seeds`: an
entry point that does not import, is not callable, or raises when asked is
skipped with a logged warning and kept in :func:`catalog_load_failures`, which
``cairn doctor`` renders.
"""
from __future__ import annotations

import logging
from importlib.metadata import entry_points
from typing import Any, Callable, Optional

_log = logging.getLogger("cairn.core.pack.model_catalogs")

ENTRY_POINT_GROUP = "cairn.model_catalogs"

#: ``(model_id) -> entry | None`` — see the module docstring.
CatalogProvider = Callable[[str], Optional[Any]]

#: Entry points that did not produce a provider on the last discovery pass, as
#: ``{name, target, error}`` (the shape ``pack_load_failures`` uses).
_LOAD_FAILURES: list[dict[str, str]] = []


def catalog_load_failures() -> list[dict[str, str]]:
    """Entry points that did not produce a provider on the last discovery pass."""
    return list(_LOAD_FAILURES)


def load_catalog_providers() -> dict[str, CatalogProvider]:
    """Every installed ``cairn.model_catalogs`` provider, by entry-point name, in
    name order. Read fresh on every call; a broken entry point is skipped and
    recorded, never raised."""
    _LOAD_FAILURES.clear()
    out: dict[str, CatalogProvider] = {}
    for ep in sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda e: (e.name, e.value)):
        try:
            obj = ep.load()
        except Exception as e:  # noqa: BLE001 — a broken provider must not take the door down
            _log.warning("model catalogue %r (%s) does not load: %s", ep.name, ep.value, e)
            _LOAD_FAILURES.append({"name": ep.name, "target": ep.value, "error": f"{type(e).__name__}: {e}"})
            continue
        if not callable(obj):
            _log.warning("model catalogue %r (%s) is not callable — skipping", ep.name, ep.value)
            _LOAD_FAILURES.append({"name": ep.name, "target": ep.value, "error": "not callable"})
            continue
        out[ep.name] = obj
    return out


def lookup(model_id: str) -> Optional[Any]:
    """The first catalogue entry any installed provider has for ``model_id``;
    ``None`` when no provider knows it — or none is installed."""
    for name, provider in load_catalog_providers().items():
        try:
            entry = provider(model_id)
        except Exception as e:  # noqa: BLE001 — one failing catalogue does not refuse the launch
            _log.warning("model catalogue %r failed for %r: %s", name, model_id, e)
            continue
        if entry is not None:
            return entry
    return None


__all__ = ["ENTRY_POINT_GROUP", "CatalogProvider", "catalog_load_failures", "load_catalog_providers", "lookup"]
