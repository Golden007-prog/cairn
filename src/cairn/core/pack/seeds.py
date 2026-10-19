"""Demo-data providers, discovered through the ``cairn.seeds`` entry-point group
(ADR-0120 D4, #2418).

``cairn demo`` and ``cairn dev --seed`` fill an opt-in demo workspace. WHAT they
fill it with belongs to whoever ships the pipelines being shown, so the core
names no pipeline itself: every provider is an entry point.

    [project.entry-points."cairn.seeds"]
    hello-cairn = "cairn.core.demo_seed:seed_hello_cairn"

A provider is an async callable::

    async def seed(workspace_id: str, *, now, runs, pipelines, reviews, registry,
                   **stores) -> SeedReport

``now`` anchors every timestamp; ``runs`` / ``pipelines`` / ``reviews`` /
``registry`` are the run store, pipeline store, review store and workflow
registry the HTTP API reads. Any other keyword (``suggestions``, ``ledger``,
``history`` today) is a store the caller passed through, possibly ``None``; a
provider that needs one it was not given builds its own default and must accept
(and ignore) keywords it does not know. The seeder runs every provider inside
the demo workspace (``current_workspace()`` is ``workspace_id``). A provider is
idempotent: deterministic ids, upserts, rows written only when absent.

The showcase (``POST /demo/seed?showcase=1``, #1969) names ONE provider,
:data:`SHOWCASE_PROVIDER`, and calls it as ``provider(workspace_id, showcase=True)``
— no stores: the provider seeds the demo workspace itself (every provider, then
every other surface it owns, through the process stores) and reports what it wrote
in :attr:`SeedReport.counts`. With that provider absent the route answers 404
(#2421: the showcase is the platform's).

Discovery fails open like :mod:`cairn.core.pack.loader`: an entry point that
does not import, or is not callable, is skipped with a logged warning and kept
in :func:`seed_load_failures`, which ``cairn doctor`` renders. An empty group is
not an error here; :func:`cairn.core.demo_seed.seed_demo_workspace` warns that
it seeded nothing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Awaitable, Callable

_log = logging.getLogger("cairn.core.pack.seeds")

ENTRY_POINT_GROUP = "cairn.seeds"

#: The provider ``POST /demo/seed?showcase=1`` asks for the full showcase.
SHOWCASE_PROVIDER = "platform-demo"

#: ``async (workspace_id, **stores) -> SeedReport`` — see the module docstring.
SeedProvider = Callable[..., Awaitable["SeedReport"]]

#: Entry points that did not produce a provider on the last discovery pass, as
#: ``{name, target, error}`` (the shape ``pack_load_failures`` uses).
_LOAD_FAILURES: list[dict[str, str]] = []


@dataclass
class SeedReport:
    """What one provider wrote: ids only, for the CLI, ``POST /demo/seed`` and tests."""

    #: Workflow / pipeline refs installed into the workspace.
    enabled: list[str] = field(default_factory=list)
    #: Leg runs written (each a leg of a seeded pipeline).
    runs: list[str] = field(default_factory=list)
    #: Pipeline records written.
    pipelines: list[str] = field(default_factory=list)
    #: Gates left waiting on a person, as ``<pipeline_id>/<step_id>``.
    pending_gates: list[str] = field(default_factory=list)
    #: What the provider could not seed on this deployment (e.g. a pipeline that
    #: is not installed), one human-readable entry each.
    skipped: list[str] = field(default_factory=list)
    #: A showcase's tally, by surface (``runs``, ``series``, ``judges``, …); empty
    #: on an ordinary seed.
    counts: dict[str, int] = field(default_factory=dict)


def seed_load_failures() -> list[dict[str, str]]:
    """Entry points that did not produce a provider on the last discovery pass."""
    return list(_LOAD_FAILURES)


def load_seed_providers() -> dict[str, SeedProvider]:
    """Every installed ``cairn.seeds`` provider, by entry-point name, in name order.

    Read fresh on every call (seeding is rare; an install between two calls is
    seen). A broken entry point is skipped and recorded, never raised."""
    _LOAD_FAILURES.clear()
    out: dict[str, SeedProvider] = {}
    targets: dict[str, str] = {}
    for ep in sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda e: (e.name, e.value)):
        if ep.name in targets:
            if targets[ep.name] == ep.value:
                continue  # one distribution seen twice on sys.path: the same provider
            # two distributions claim one name: the first (by sort) wins, loudly
            _log.warning("seed provider %r (%s) skipped: the name is already taken by %s",
                         ep.name, ep.value, targets[ep.name])
            _LOAD_FAILURES.append({"name": ep.name, "target": ep.value,
                                   "error": "duplicate provider name"})
            continue
        try:
            obj: Any = ep.load()
        except Exception as e:  # noqa: BLE001 — a broken provider must not break `cairn demo`
            _log.warning("failed to load seed provider %r (%s): %s", ep.name, ep.value, e)
            _LOAD_FAILURES.append({"name": ep.name, "target": ep.value,
                                   "error": f"{type(e).__name__}: {e}"})
            continue
        if not callable(obj):
            _log.warning("seed provider %r resolved to %r, not a callable — skipping",
                         ep.name, type(obj).__name__)
            _LOAD_FAILURES.append({"name": ep.name, "target": ep.value,
                                   "error": f"entry point resolved to {type(obj).__name__}, not a callable"})
            continue
        out[ep.name], targets[ep.name] = obj, ep.value
    return out


__all__ = ["ENTRY_POINT_GROUP", "SHOWCASE_PROVIDER", "SeedProvider", "SeedReport", "load_seed_providers", "seed_load_failures"]
