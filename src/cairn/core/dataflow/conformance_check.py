"""ADR-0118 D5 — the ONE contract validator, over self-contained schemas.

Every shared-contract check runs through :func:`problems`: the operator seam
(``ContractEnvelope.problems`` delegates here), the control plane's stage checks
(``execute_leg`` and ``dataflow/run``), and the interpreter on the job machine
(``run_dataflow``). The box imports this package already and needs only
``jsonschema`` (the ``[kernel]`` extra): the launch door bundles each part into one
self-contained schema (:func:`bundle`), so no registry, ``referencing`` resource or
fetch is involved here.

The submission-site helpers below (:func:`stamp`, :func:`submission_problems`,
:func:`descriptor_problems`) are shared by the two doors that submit a spec — the
``flow:`` leg executor (core) and the ``dataflow/run`` operator (an operator pack) —
so the doors cannot drift.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from jsonschema import Draft202012Validator

from cairn.core.dataflow.contract import DataflowSpec, SpecContracts, StageContract
from cairn.core.dataflow.engine import carries_stage_contracts

_DIALECT = "https://json-schema.org/draft/2020-12/schema"
#: D4 — the spec travels in ONE environment string, which Linux caps at 128 KiB.
MAX_SPEC_BYTES = 96 * 1024


class StageContractError(ValueError):
    """An opted-in stage failed its admitted stage contract (identity, configuration,
    output, size, engine declaration or lost provenance). It fails the stage and its leg."""


def _references(schema: Any) -> set[str]:
    """Every ``contract://`` document a schema's ``$ref``s name."""
    found: set[str] = set()
    stack = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("contract://"):
                found.add(ref.partition("#")[0])
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def bundle(schemas: dict[str, Any], uri: str) -> dict[str, Any]:
    """One self-contained schema for ``uri``: it and every schema it reaches through
    ``$ref`` are inlined into one document as embedded resources (each under its own
    ``$id``), so every reference resolves inside the document — JSON Schema 2020-12
    bundling, which also keeps a recursive schema valid. Deterministic: sorted URIs."""
    reached: dict[str, Any] = {}
    pending = [uri]
    while pending:
        current = pending.pop()
        if current in reached:
            continue
        body = schemas[current]
        body = ({} if body else {"not": {}}) if isinstance(body, bool) else dict(body)
        reached[current] = {**body, "$id": current}
        pending.extend(_references(body) - reached.keys())
    return {"$schema": _DIALECT, "$ref": uri, "$defs": {u: reached[u] for u in sorted(reached)}}


def problems(schema: Any, value: Any, part: str) -> list[str]:
    """Validation messages for ``value`` against a self-contained ``schema``; empty = conforms."""
    validator = Draft202012Validator(schema)
    return [f"{part}{'/' + '/'.join(str(x) for x in e.absolute_path) if e.absolute_path else ''}: {e.message}"
            for e in sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path))]


def node_stage_contracts(recorded: Optional[dict], node: str = "") -> Optional[dict]:
    """The carrier one door stamps from: ``{"leg", "stages": {<index>: entry}}`` for the
    flow leg itself (``node=""``) or one ``dataflow/run``/``ml/run`` node of a template leg,
    out of the leg's recorded map ``{"leg", "nodes": {<node path>: {...}}}`` (or its
    ``error``). ``None`` — no record — is legacy."""
    if recorded is None:
        return None
    return {"leg": recorded["leg"], "error": recorded.get("error", ""),
            "stages": dict((recorded.get("nodes") or {}).get(node) or {})}


def stamp(spec: DataflowSpec, stage_contracts: Optional[dict],
          implements: Callable[[str], tuple]) -> DataflowSpec:
    """ADR-0118 D4/D6 — copy the admitted stage contracts onto the spec, before
    ``checkpoint_key``. ``None`` (the dev loop, an unadmitted template) leaves the spec
    legacy; so does an admitted leg with no opted-in stage, whose bytes stay unchanged.
    Inside an admitted leg, an opted-in stage without its entry is lost provenance (M2a)."""
    if stage_contracts is None:
        return spec
    leg = stage_contracts["leg"]
    if stage_contracts.get("error"):
        raise StageContractError(stage_contracts["error"])
    entries = dict(stage_contracts.get("stages") or {})
    stages, schemas = [], {}
    for i, ref in enumerate(spec.stages):
        entry = entries.pop(str(i), None)
        if entry is None:
            if implements(ref.stage):
                raise StageContractError(
                    f"stage {i} ({ref.stage!r}) implements {list(implements(ref.stage))} but leg {leg!r} was "
                    "admitted with no contract for it; refusing to run unrecorded")
            stages.append(ref)
            continue
        if entry["stage"] != ref.stage:
            raise StageContractError(f"stage {i} is {ref.stage!r}, but leg {leg!r} was admitted with {entry['stage']!r}")
        schemas.update(entry["schemas"])
        stages.append(ref.model_copy(update={"contract": StageContract(**entry["contract"])}))
    if entries:
        raise StageContractError(f"leg {leg!r} was admitted with stage(s) {sorted(entries)} the submitted spec does not run")
    if not schemas:
        return spec
    return spec.model_copy(update={"stages": stages, "contracts": SpecContracts(leg=leg, schemas=schemas)})


def submission_problems(spec: DataflowSpec, engine: Any) -> list[str]:
    """D4/D5/D7 at a submission site, before any attempt or reservation: the engine must
    declare it carries stage contracts, the serialized spec must fit the environment
    string, and each opted-in (unmapped) stage's resolved config must conform. A mapped
    stage's per-item config is only known on the box; the interpreter checks it there."""
    if spec.contracts is None:
        return []
    out = []
    if not carries_stage_contracts(engine):
        out.append(f"engine {getattr(engine, 'name', engine)!r} does not declare carries_stage_contracts; "
                   "refusing an opted-in spec (ADR-0118 D7)")
    size = len(spec.model_dump_json().encode("utf-8"))
    if size > MAX_SPEC_BYTES:
        out.append(f"the opted-in spec is {size} bytes, over the {MAX_SPEC_BYTES}-byte bound (ADR-0118 D4)")
    for i, ref in enumerate(spec.stages):
        if ref.contract is not None and ref.map is None:
            out += [f"stage {i} ({ref.stage}): {p}" for p in
                    problems(spec.contracts.schemas[ref.contract.configuration], dict(ref.config), "configuration")]
    return out


def descriptor_problems(stage_contracts: Optional[dict], stages_total: int, descriptors: list) -> list[str]:
    """D5 on the result, before registration: each opted-in stage's descriptor stats
    (one descriptor per stage, in order) re-checked against its output schema."""
    entries = (stage_contracts or {}).get("stages") or {}
    if not entries:
        return []
    if len(descriptors) != stages_total:
        return [f"{len(descriptors)} descriptor(s) for {stages_total} stage(s); the stage outputs cannot be re-checked"]
    out = []
    for index, entry in sorted(entries.items(), key=lambda kv: int(kv[0])):
        stats = dict(getattr(descriptors[int(index)], "stats", None) or {})
        out += [f"stage {index} ({entry['stage']}): {p}" for p in
                problems(entry["schemas"][entry["contract"]["output"]], stats, "output")]
    return out


__all__ = ["MAX_SPEC_BYTES", "StageContractError", "bundle", "descriptor_problems", "node_stage_contracts",
           "problems", "stamp", "submission_problems"]
