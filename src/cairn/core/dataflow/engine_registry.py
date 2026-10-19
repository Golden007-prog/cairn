"""Which engine runs this flow — the core-owned half of the engine waist (#1512).

:mod:`cairn.core.dataflow.engine` defines *what* an engine is (the
``DataflowEngine`` protocol). This module answers *which one to use*, and it
lives in core for the reason ADR-0084 D1 gives: runtimes are adapters behind a
core-owned waist, so core must be able to pick one without importing any
particular adapter. Before this, ``cairn.core.dispatch.pipeline_bridge`` and the
Temporal pipeline activities both did::

    from operator_dataflow.engines import resolve_engine   # core -> pack

which made one specific operator pack a hard requirement for dispatching *any*
flow leg, in the import direction the architecture forbids.

**Core owns selection; packs own construction.** That split is deliberate, and
it is not the split the issue originally proposed ("move ``resolve_engine`` into
core"): each engine is built from *engine-specific* configuration — the Metaflow
adapter alone reads eight ``metaflow_*`` backend keys — so moving construction
into core would drag far more of the pack into core than the import it removed.
What core can own is the part that is genuinely engine-independent: read
``<kind>_engine`` / ``<kind>_target`` through the one config plane (#1093), then
hand the name to whichever pack registered it.

Registration is the ``cairn.dataflow_engines`` entry-point group, mirroring
``cairn.vector_stores`` (:mod:`cairn.services.rag.stores`) and
``cairn.dataflow_stages``. A pack exposes a factory per engine name::

    [project.entry-points."cairn.dataflow_engines"]
    builtin  = "operator_dataflow.engines:builtin_engine"
    metaflow = "operator_dataflow_metaflow.factory:metaflow_engine"   # its own wheel, ADR-0134 D2

Each factory takes ``(*, kind, target)`` and reads whatever else it needs from
the backend-selection plane itself.

A selected-but-uninstalled engine raises — it never silently falls back to the
builtin one. That is the #568 fail-loud discipline: a flow that quietly ran
somewhere other than where the environment said it should is worse than a flow
that refused to start.
"""
from __future__ import annotations

from typing import Any, Optional

from cairn.core.dataflow.engine import EngineError

ENTRY_POINT_GROUP = "cairn.dataflow_engines"

#: Backend-selection keys per flow kind (ADR-0082 D7, #1074). Engines are
#: KIND-BLIND — they interpret a stage list via the shared kernel registry — so
#: the same adapter serves both kinds and the environment maps kinds to engines
#: many-to-many.
_KEYS: dict[str, tuple[str, str]] = {
    "ml": ("ml_engine", "ml_target"),
    "data": ("dataflow_engine", "dataflow_target"),
}


def selection_keys(kind: str = "data") -> tuple[str, str]:
    """``(engine_key, target_key)`` — the two backend-selection fields this flow
    kind resolves through.

    Public since #2134 S4: the resolved view reports which layer decided a leg's
    engine and target, and it must ask about the SAME two keys
    :func:`selected_engine` reads, not a copy of this mapping.
    """
    return _KEYS.get(kind, _KEYS["data"])


def installed_engines() -> list[str]:
    """Engine names any installed pack registers, sorted. Empty when no engine
    pack is installed — which is a legitimate state for a control plane that
    only runs agentic workflows, so this reports rather than raises."""
    from importlib.metadata import entry_points

    return sorted({ep.name.lower() for ep in entry_points(group=ENTRY_POINT_GROUP)})


def load_engine_factory(name: str):
    """The registered factory for ``name``, or ``None`` when nothing provides it.

    Core never imports an engine module directly; ``ep.load()`` is what pulls the
    adapter (and its optional extra — metaflow, skypilot, paramiko) into the
    process, and only for the engine actually selected.
    """
    from importlib.metadata import entry_points

    wanted = (name or "").strip().lower()
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        if ep.name.lower() == wanted:
            return ep.load()
    return None


def selected_engine(kind: str = "data") -> tuple[str, str]:
    """``(engine_name, target)`` for this flow kind, through the ONE config plane
    (#1093): profile record → ``OBS_*`` env override → schema default.

    Split out from :func:`resolve_engine` so callers that want to *report* the
    selection — doctor, the console's environment view, a dry-run verdict — can
    read it without constructing an engine (and so without needing the pack that
    provides it installed).
    """
    engine_key, target_key = selection_keys(kind)
    try:
        from cairn.core.backend_selection import selected
    except Exception:  # noqa: BLE001 — no selection plane here: builtin is the contract
        return "builtin", ""
    choice = (selected(engine_key, default="builtin") or "builtin").strip().lower()
    target = (selected(target_key, default="") or "").strip()
    return choice or "builtin", target


def resolve_engine(ctx: Any = None, kind: str = "data") -> Any:
    """The engine for a flow of this ``kind`` (ADR-0081 D4, #1050; kind axis
    ADR-0082 D7, #1074). Callers never construct engines themselves.

    ``ctx`` is accepted and ignored — it is part of the operator-side signature
    this replaced, and keeping it means the pack's ``resolve_engine`` can
    delegate here without changing any of its call sites.
    """
    choice, target = selected_engine(kind)
    factory = load_engine_factory(choice)
    if factory is None:
        raise EngineError(_not_installed_message(choice, kind))
    return factory(kind=kind, target=target)


def _not_installed_message(choice: str, kind: str) -> str:
    """Say what was selected, what IS available, and which knob to turn.

    The names come from what is actually installed rather than a hardcoded list,
    so the message stays true as engine packs are added or removed — the old
    hardcoded "builtin, metaflow, prefect" claimed prefect was available on a
    deploy that had never installed it.
    """
    engine_key = _KEYS.get(kind, _KEYS["data"])[0]
    available = installed_engines()
    have = ", ".join(available) if available else (
        "none — no pack registers a `cairn.dataflow_engines` entry point "
        "(install cairn-operator-dataflow)"
    )
    return (
        f"{kind} flow engine {choice!r} is selected but not installed — "
        f"available: {have}. Set [profile.backends] {engine_key} to an "
        f"installed engine, or install the pack that provides {choice!r}."
    )


def engine_for_name(name: str, *, kind: str = "data", target: str = "") -> Optional[Any]:
    """Construct a named engine directly, bypassing selection. For conformance
    tests and tooling that must exercise a specific adapter; ``None`` when that
    engine is not installed."""
    factory = load_engine_factory(name)
    return None if factory is None else factory(kind=kind, target=target)
