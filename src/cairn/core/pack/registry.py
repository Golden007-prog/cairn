"""Global registry of loaded packs + aggregation helpers.

The engine's consumption points (tool adapter, settings inventory, doctor,
template resolver) read from here rather than importing any pack module. The
registry is process-global and keyed by pack name; registering the same name
twice replaces the earlier entry (last writer wins — supports editable-reinstall
during dev).
"""

from __future__ import annotations

import copy
from typing import Any, Iterable, Optional, TYPE_CHECKING

from cairn.core.refs import pack_key  # ONE ref spelling (#2126); pure stdlib, no cycle

if TYPE_CHECKING:
    from pathlib import Path

    from cairn.core.pack.composition import CompositionReport
    from cairn.core.pack.contracts import ResolvedDefinition
    from cairn.core.pack.protocol import (
        BackendDescriptor,
        CapabilityBinding,
        Pack,
        ToolDescriptor,
    )


_PACKS: dict[str, "Pack"] = {}
#: Last composition report produced by the loader. Doctor (P2.c) reads
#: this; tests reset it via :func:`clear`. ``None`` means the loader
#: hasn't run yet — distinct from "ran and found no conflicts".
_LAST_REPORT: "Optional[CompositionReport]" = None  # noqa: F821


def register_pack(pack: "Pack") -> None:
    """Register (or replace) a pack by name.

    §Phase-2.8: populates ``pack.trigger_specs`` from the pack's
    ``triggers_dir`` if the pack didn't pre-fill it. Each pack
    constructor *may* pass ``trigger_specs`` explicitly (tests do, to
    bypass disk I/O); when not supplied, we walk
    :func:`cairn.core.engine.triggers_spec.load_trigger_specs` against the
    declared directory. The Pack dataclass is frozen, so we replace
    rather than mutate.
    """
    from dataclasses import replace
    from pathlib import Path

    if not pack.trigger_specs and pack.triggers_dir:
        triggers_path = Path(pack.triggers_dir)
        if triggers_path.is_dir():
            from cairn.core.engine.triggers_spec import load_trigger_specs
            specs = load_trigger_specs(triggers_path)
            if specs:
                pack = replace(pack, trigger_specs=specs)

    _PACKS[pack.name] = pack


def get_packs() -> list["Pack"]:
    """All registered packs, in registration order."""
    return list(_PACKS.values())


def get_pack(name: str) -> "Pack | None":
    p = _PACKS.get(name)
    if p is not None:
        return p
    # #970 — the console addresses packs by their DIRECTORY name (underscore,
    # e.g. `dataset_readiness`), but packs register under their pack.toml `name`
    # (hyphen, `dataset-readiness`). Fall back to a separator-insensitive match
    # so /packs/{ref}/contract|graph|run resolve regardless of `_` vs `-`.
    # #2126 — through the ONE normaliser, not a private copy of the rule.
    norm = pack_key(name)
    for key, pk in _PACKS.items():
        if pack_key(key) == norm:
            return pk
    return None


def flow_dirs_of(pack: Any) -> list[str]:
    """Every dir a pack keeps agentic flows in — `flows/` AND the `templates/`
    alias (#1163).

    One helper because the alias has to be honoured EVERYWHERE `templates_dir`
    is read, not just in the loader. It was not: `pack_owning_template` matched
    only `templates_dir`, so a pack migrated to `flows/` lost its within-pack
    visibility privilege and fell back to permissive global resolution — a
    silent encapsulation regression that shipped with #1133.
    """
    out = []
    for attr in ("flows_dir", "templates_dir"):
        d = getattr(pack, attr, None)
        if d:
            out.append(str(d))
    return out


def pack_owning_template(template_path: "Path | str") -> str | None:
    """Reverse lookup: which registered pack owns the template at this path?

    A template lives at ``<pack>/templates/<name>.md``, so its owner is the
    pack whose ``templates_dir`` is the file's parent directory. Returns
    ``None`` for templates outside any registered pack (examples/, tests/,
    ad-hoc dirs) — callers then fall back to permissive (global) resolution.

    §24.3 P2.e — the compiler uses this to resolve subgraph refs under the
    *calling* pack's identity, so the within-pack visibility rule can fire
    (a pack may subgraph its own unlisted internal templates; an encapsulated
    pack's internals stay invisible to other packs).
    """
    from pathlib import Path

    p = Path(template_path).resolve()
    for pack in _PACKS.values():
        for td in flow_dirs_of(pack):
            if Path(td).resolve() == p.parent:
                return pack.name
    return None


def clear() -> None:
    """Drop all registered packs. For tests + reload."""
    global _LAST_REPORT
    _PACKS.clear()
    _LAST_REPORT = None


# ─── Composition report (§24.3 / P2.b) ───────────────────────────────


def set_composition_report(report: "CompositionReport") -> None:
    """Store the loader's last validation pass. Read by doctor (P2.c)."""
    global _LAST_REPORT
    _LAST_REPORT = report


def get_composition_report() -> "Optional[CompositionReport]":
    """Return the cached report, or ``None`` if the loader hasn't run."""
    return _LAST_REPORT


# ─── Aggregation across all registered packs ────────────────────────


def all_tools() -> list["ToolDescriptor"]:
    out: list[ToolDescriptor] = []
    for pack in _PACKS.values():
        out.extend(pack.tools)
    return out


def all_capability_bindings() -> list["CapabilityBinding"]:
    """Every capability binding contributed by every registered pack.

    Same registry-walk pattern as ``all_tools()``, but yielding the bare
    callables instead of agent-loop StructuredTool factories. (Its MCP
    consumer, ``services/mcp/server.py``, was retired in #2425.)
    """
    out: list[CapabilityBinding] = []
    for pack in _PACKS.values():
        out.extend(pack.capability_bindings)
    return out


def all_contract_definitions() -> tuple["ResolvedDefinition", ...]:
    """Every definition of every installed contract distribution, closed offline (#2240 B2).

    A facade only: delegates to ``contracts.resolve_contracts`` and keeps no scan or
    state of its own. An empty installation is ``()``; one invalid distribution refuses
    the whole result rather than publishing a partial inventory.
    """
    from cairn.core.pack.contracts import resolve_contracts  # noqa: PLC0415 — keep contracts off the registry import path
    return tuple(definition for distribution in resolve_contracts() for definition in distribution.definitions)


def contract_resolution_snapshot() -> dict[str, Any]:
    """The installed contract closure as ``cairn.resolution/1`` data (#2240 C): names,
    versions and digests only — never local paths. Same discipline as
    :func:`all_contract_definitions`: delegates to ``resolve_contracts``, keeps no
    state, and one invalid distribution refuses (``ContractDiscoveryError``) rather
    than publishing a partial closure. The engine's ``ContractResolution`` types it;
    dispatch assigns it to the plan at the launch door.
    """
    from cairn.core.pack.contracts import resolve_contracts  # noqa: PLC0415 — keep contracts off the registry import path
    resolved = resolve_contracts()
    return {
        "format": "cairn.resolution/1",
        "distributions": [{"name": d.name, "version": d.version, "digest": d.digest} for d in resolved],
        "definitions": [{"id": x.id, "revision": x.revision, "kind": x.kind, "distribution": d.name,
                         "version": d.version, "digest": x.digest} for d in resolved for x in d.definitions],
    }


def _closure_bodies() -> dict[str, tuple[Any, object]]:
    """Every installed contract definition, closed (B2) and read (B1), by URI: the bodies
    ``contracts`` keeps with the installed resolution, from the read that was digested.
    Shared: callers copy a body before handing it on."""
    from cairn.core.pack.contracts import installed_contract_bodies  # noqa: PLC0415 — keep contracts off the registry import path
    return installed_contract_bodies()


def _parts(kind: str) -> tuple[str, ...]:
    """ADR-0118 A2 — the schema parts a definition has: a capability's input, configuration
    and output; a stage contract's configuration and output (rows are not contracted)."""
    return ("input", "configuration", "output") if kind == "capability" else ("configuration", "output")


def invocation_definitions(capability_ref: str, kind: str = "capability") -> dict[str, Any]:
    """#2241 B, for the launch door: the capability definition and the definitions it
    references (input/configuration/output schemas, report) as ``ContractDefinitionRef``
    data with digests, plus the report spec for the reports-equality check. Refuses an
    unknown reference or one of another ``kind`` (ADR-0118 D1: an operation implements a
    capability, a stage a ``stage`` contract, never the other). Keeps no state of its own.
    """
    from cairn.core.pack.contracts import ContractDiscoveryError  # noqa: PLC0415 — as above
    bodies = _closure_bodies()
    if capability_ref not in bodies or bodies[capability_ref][0].kind != kind:
        raise ContractDiscoveryError(f"{capability_ref} is not an installed {kind} definition")
    capability = bodies[capability_ref][1]
    refs = [capability_ref] + [capability[k] for k in _parts(kind)]
    refs += [capability["report"]] if "report" in capability else []
    missing = [r for r in refs if r not in bodies]
    if missing:
        raise ContractDiscoveryError(f"{capability_ref} references definitions that are not installed: {missing}")

    fields = ("id", "revision", "kind", "distribution", "version", "digest")
    return {"definitions": [{f: getattr(bodies[u][0], f) for f in fields} for u in refs],
            "report_spec": copy.deepcopy(bodies[capability["report"]][1]["spec"]) if "report" in capability else None}


def invocation_contract(definitions: Iterable[tuple[str, str]], closure: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """#2241 B, for a leg: the ``ContractEnvelope`` data for a recorded ``InvocationEntry``.
    Bodies are re-read from the installed closure and EVERY one that rides along —
    the entry's definitions and the schemas its input/configuration/output reach through
    ``$ref`` — is digest-checked against what the launch door recorded (``closure`` is
    the record's ``contract_resolution.definitions``), so a changed installation refuses
    instead of silently re-resolving (ADR-0111 §4). The caller adds the recorded operator
    identity.
    """
    from cairn.core.pack.contracts import ContractDiscoveryError, _schema_positions  # noqa: PLC0415 — as above
    bodies = _closure_bodies()
    entry = dict(definitions)
    recorded = dict(closure)
    recorded.update(entry)

    def checked(uri: str) -> object:
        if uri not in bodies or bodies[uri][0].digest != recorded.get(uri):
            raise ContractDiscoveryError(f"installed definition {uri} is missing or differs from the recorded resolution")
        return copy.deepcopy(bodies[uri][1])

    # the ENTRY's own definition — never the first capability of the whole closure
    capability_uri = next((uri for uri in entry
                           if uri in bodies and bodies[uri][0].kind in ("capability", "stage")), None)
    if capability_uri is None:
        raise ContractDiscoveryError("the recorded invocation names no installed capability or stage definition")
    capability = checked(capability_uri)
    refs = {k: capability[k] for k in _parts(bodies[capability_uri][0].kind)}
    schemas: dict[str, Any] = {}
    pending = list(refs.values())
    while pending:
        uri = pending.pop()
        if uri in schemas:
            continue
        schemas[uri] = checked(uri)
        for position in _schema_positions(schemas[uri]):
            target = position.get("$ref", "").partition("#")[0]
            if target.startswith("contract://") and target not in schemas:
                pending.append(target)
    return {"capability_ref": capability_uri, "refs": refs, "schemas": schemas}


def all_backends() -> list["BackendDescriptor"]:
    out: list[BackendDescriptor] = []
    for pack in _PACKS.values():
        out.extend(pack.backends)
    return out


def all_settings_inventory() -> list:
    """Merged settings specs contributed by every pack."""
    out: list = []
    for pack in _PACKS.values():
        out.extend(pack.settings_inventory)
    return out


def all_doctor_checks() -> list:
    out: list = []
    for pack in _PACKS.values():
        out.extend(pack.doctor_checks)
    return out


# Asset-dir aggregators: one per Pack.*_dir field. Each returns the set of
# absolute paths every registered pack exposes for that asset kind, in
# registration order, with packs that don't ship the kind skipped. Used by
# the engine's template loader, skill engine, policy loader, and the
# upcoming schema / trigger / eval / rag / memory / examples / data
# consumers introduced in Phase 1 of the pack-structure refactor.
def _all_dirs(attr: str) -> list[str]:
    return [d for p in _PACKS.values() if (d := getattr(p, attr, None))]


def all_flow_dirs() -> list[str]:
    """Every installed pack's ``flows/`` dir (#1133) — agentic flows live here
    as ``.md`` alongside data/ml flows as ``.yaml``."""
    return _all_dirs("flows_dir")


def all_pipeline_dirs() -> list[str]:
    """Every installed pack's ``pipelines/`` dir (#1125) — the discovery source
    for pipeline install, mirroring :func:`all_template_dirs`."""
    return _all_dirs("pipelines_dir")


def all_preset_dirs() -> list[str]:
    """Every installed pack's ``presets/`` dir (#1789, ADR-0071 §6) — the
    discovery source for pack-shipped SEED launch presets, mirroring
    :func:`all_pipeline_dirs`."""
    return _all_dirs("presets_dir")


def all_template_dirs() -> list[str]:
    # #1163 — "where do agentic flows live": `flows/` AND the `templates/`
    # alias, deduped and in registration order. After the pack migration no
    # first-party pack declares templates_dir, so returning only that made every
    # consumer (pack contract API, catalog, resolver) see nothing at all.
    seen: list[str] = []
    for d in _all_dirs("flows_dir") + _all_dirs("templates_dir"):
        if d not in seen:
            seen.append(d)
    return seen


def all_skill_dirs() -> list[str]:
    return _all_dirs("skills_dir")


def all_policy_dirs() -> list[str]:
    return _all_dirs("policies_dir")


# Phase 1 additions — declarative (schemas, triggers, evals) and asset
# (rag, memory, examples, data) directory aggregators.

def all_schemas_dirs() -> list[str]:
    return _all_dirs("schemas_dir")


def all_triggers_dirs() -> list[str]:
    return _all_dirs("triggers_dir")


def all_evals_dirs() -> list[str]:
    return _all_dirs("evals_dir")


def all_rag_dirs() -> list[str]:
    return _all_dirs("rag_dir")



def all_examples_dirs() -> list[str]:
    return _all_dirs("examples_dir")


def all_data_dirs() -> list[str]:
    return _all_dirs("data_dir")
