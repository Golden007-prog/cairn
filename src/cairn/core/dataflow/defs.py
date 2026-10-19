"""First-class pack flows (ADR-0081 D2 #1051, generalized by ADR-0082 D7 #1073).

A flow graduates from an inline stage list in an operation binding to a
declared pack DATA artifact: ``<pack>/flows/<name>.yaml`` with a ``kind:``
discriminator (``data`` | ``ml``), addressable as ``<pack>/<name>`` through a
KIND-MATCHED door operator — ``dataflow/run`` runs ``kind: data`` flows,
``ml/run`` runs ``kind: ml`` flows. The legacy ``dataflows/`` dir remains a
back-compat alias that only holds ``kind: data``. This module is the
control-plane loader/validator/resolver — the point where the compiler can
read the spec and enforce safety STRUCTURALLY (a destructive flow cannot
ship ungated), identically for every kind and every engine.

Stage capability + risk discovery reads the kernel entry-point GROUPS
directly — the group is the contract (ADR-0081 §5), so core never imports the
operator wheels. ONE registry serves every flow kind and every engine
(kernels are engine-blind; ADR-0082 D7). Risk is fail-closed: a stage that
does not declare ``risk_class`` counts as ``external_write`` (#744
discipline).

Run-specific values reach a static def through ``$config.<key>`` placeholders,
substituted from the spec's ``config`` dict at resolution — deterministic
string replacement, not a templating engine.
"""
from __future__ import annotations

import logging
import re
from functools import lru_cache
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

from cairn.core.dataflow.contract import StageRef
from cairn.core.engine.expr import Scopes, resolve_value  # ONE language (#1118)
from cairn.core.refs import bare_ref, pick  # ONE ref spelling (#2126)

_log = logging.getLogger("cairn.core.dataflow.defs")

# Legacy group name — still the primary registry every existing kernel uses.
STAGE_ENTRY_POINT_GROUP = "cairn.dataflow_stages"
# Forward-neutral alias group (ADR-0082 D7): kernels serving any flow kind
# may register here instead; both groups feed ONE merged registry.
FLOW_STAGE_ENTRY_POINT_GROUP = "cairn.flow_stages"

FLOW_KIND_DATA = "data"
FLOW_KIND_ML = "ml"

RISK_READ_ONLY = "read_only"
RISK_EXTERNAL_WRITE = "external_write"


class DataflowDefError(ValueError):
    """A flow def failed to load/validate/resolve — message carries
    pack/flow/stage coordinates."""


class FlowHandoff(BaseModel):
    """Stage pair that materializes the dataset across an in-flow gate (#1079)."""

    model_config = ConfigDict(extra="forbid")

    export: str = Field(..., min_length=1, description="Stage that writes the handoff artifact.")
    ingest: str = Field(..., min_length=1, description="Stage that reads it back.")


class FlowDef(BaseModel):
    """One ``flows/<name>.yaml`` (or legacy ``dataflows/<name>.yaml``) —
    pack DATA, hashed by ``cairn lock``.

    ``kind`` names the ARTIFACT the flow exists to produce (ADR-0082 D7):
    ``data`` → dataset artifact (dataflow) · ``ml`` → model artifact (ML
    flow, a flow of ML steps). Engines map to kinds in environment config,
    many-to-many — the def never names an engine."""

    name: str
    kind: Literal["data", "ml"] = FLOW_KIND_DATA
    description: str = ""
    stages: list[StageRef] = Field(..., min_length=1)
    # #1079 — how the working dataset crosses an in-flow gate. Segments run as
    # SEPARATE engine jobs with separate workdirs, so the boundary must be
    # materialized: the export stage closes one segment, the ingest opens the
    # next, both pointing at the same handoff path. Required only when a stage
    # declares `gate:`; every flow shipped today omits it.
    handoff: Optional["FlowHandoff"] = None
    # #2188 (T03, C2) — a flow may declare that a known check does not apply
    # to what it produces (``checks: {decontam: not_applicable}``). The leg
    # executor records it on the leg's evidence; the intake report shows it
    # beside the checks that ran. Only ``not_applicable`` is declarable — what
    # is REQUIRED is policy's, never a flow's (#2190).
    checks: dict[str, Literal["not_applicable"]] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


# Back-compat alias — #1051 callers imported the data-only name.
DataflowDef = FlowDef


# ─── stage registry introspection (entry-point groups = the contract) ─────


@lru_cache(maxsize=1)
def _stage_entry_points() -> dict[str, Any]:
    eps: dict[str, Any] = {}
    for group in (STAGE_ENTRY_POINT_GROUP, FLOW_STAGE_ENTRY_POINT_GROUP):
        for ep in entry_points(group=group):
            if ep.name in eps:
                _log.warning(
                    "kernel %r registered in both stage groups — keeping the "
                    "%r registration", ep.name, STAGE_ENTRY_POINT_GROUP,
                )
                continue
            eps[ep.name] = ep
    return eps


def known_stage_names() -> set[str]:
    return set(_stage_entry_points())


def stage_risk_class(stage: str) -> str:
    """The declared ``risk_class`` of a registered kernel; fail-closed to
    ``external_write`` when undeclared or unloadable."""
    ep = _stage_entry_points().get(stage)
    if ep is None:
        return RISK_EXTERNAL_WRITE
    try:
        fn = ep.load()
    except Exception:  # noqa: BLE001 - a broken kernel must not crash validation
        return RISK_EXTERNAL_WRITE
    risk = str(getattr(fn, "risk_class", RISK_EXTERNAL_WRITE))
    if risk not in (RISK_READ_ONLY, RISK_EXTERNAL_WRITE):
        # ADR-0089 R2 — coercion stays fail-closed, but it must not be SILENT.
        # cairn governs an engine-run flow at ADMISSION (plan-time gates + the
        # door's risk aggregate); it does not enforce per-call obligations
        # inside the flow. That boundary is sound while the vocabulary is
        # "does this mutate?" — a kernel declaring a NEW class (an LLM call, a
        # network egress) is asking a question this vocabulary cannot answer,
        # and folding it into external_write would hide that the in-flow
        # governance question just became live. Say so, loudly.
        _log.warning(
            "stage %r declares risk_class %r, which cairn does not model — "
            "treating it as %s (fail-closed). If this kernel calls an LLM or "
            "egresses data, the in-flow policy boundary (ADR-0089 R2) needs "
            "revisiting before it ships: engine-run stages are governed at "
            "admission only.",
            stage, risk, RISK_EXTERNAL_WRITE,
        )
        return RISK_EXTERNAL_WRITE
    return risk


def stage_implements(stage: str) -> tuple[str, ...]:
    """ADR-0118 D1 — the ``implements`` a registered kernel declares beside ``risk_class``
    (``contract://<id>/<rev>`` stage contracts); ``()`` is a legacy stage. Read from the
    entry point, as :func:`stage_risk_class` reads the risk; the runner reads the same
    attribute on the kernel it loads. An unloadable kernel declares nothing here and fails
    as an unknown stage where it runs."""
    ep = _stage_entry_points().get(stage)
    try:
        return tuple(getattr(ep.load(), "implements", ()) or ()) if ep is not None else ()
    except Exception:  # noqa: BLE001 - a broken kernel must not crash the launch door
        return ()


def stage_version(stage: str) -> str:
    """The distribution version of a registered kernel (the ``kernel_version`` the runner
    stamps), or ``""`` when its distribution cannot be read."""
    ep = _stage_entry_points().get(stage)
    return str(getattr(getattr(ep, "dist", None), "version", "") or "")


def stage_catalog() -> list[dict[str, str]]:
    """Every registered kernel, with the two facts an author needs: what it
    does (docstring first line) and what it may DO (:func:`stage_risk_class`,
    fail-closed) — the same per-stage number :func:`aggregate_risk` folds over.

    The registry IS the list (ADR-0081 §5): a kernel installed into the
    deployment appears here with no hand-maintained catalog to update, which is
    the whole reason the group is the contract. ``provider`` names the
    distribution the entry point came from, so the console can group by pack.
    """
    out: list[dict[str, str]] = []
    for name, ep in sorted(_stage_entry_points().items()):
        doc = ""
        try:
            fn = ep.load()
            lines = ((getattr(fn, "__doc__", "") or "").strip().splitlines() or [""])
            doc = lines[0].strip()
        except Exception:  # noqa: BLE001 - a kernel that will not import still EXISTS; risk goes fail-closed below
            doc = ""
        out.append({
            "name": name,
            "description": doc,
            "risk": stage_risk_class(name),
            "provider": str(getattr(getattr(ep, "dist", None), "name", "") or ""),
        })
    return out


def aggregate_risk(stages: list[StageRef]) -> str:
    """A flow is as risky as its riskiest stage."""
    for ref in stages:
        if stage_risk_class(ref.stage) == RISK_EXTERNAL_WRITE:
            return RISK_EXTERNAL_WRITE
    return RISK_READ_ONLY


# ─── loading pack defs ────────────────────────────────────────────────────


#: #2069 — flow defs that would not load, and why. Keyed the same as the
#: returned defs, so a caller holding a ref can ask why it is missing.
_UNRESOLVABLE: dict[str, str] = {}


def unresolvable_flow_defs() -> dict[str, str]:
    """``{flow_ref: reason}`` for defs the last load refused.

    A dropped def is indistinguishable from one nobody wrote, and the resulting
    "flow X does not resolve" names neither the stage nor the pack to install.
    This is the channel that turns that into an answer.
    """
    return dict(_UNRESOLVABLE)


def validate_def(d: FlowDef, *, where: str) -> list[str]:
    """Structural problems, each with coordinates. Registry check only —
    per-stage config schemas arrive when kernels declare them."""
    known = known_stage_names()
    return [
        f"{where}: stage {i} refs unknown kernel {ref.stage!r} "
        f"(registry has {sorted(known)})"
        for i, ref in enumerate(d.stages)
        if ref.stage not in known
    ]


def parse_flow_def(text: str, *, where: str) -> FlowDef:
    """One flow document's TEXT → a validated :class:`FlowDef`.

    The parser, split out of :func:`_load_def_file` so a caller holding the
    bytes (the console builder's ``POST /builder/parse-flow``, #1771) reads the
    grammar through the SAME function the pack loader does. A second parser
    would be a second answer to "is this a valid flow" — the #1376 lesson: a
    chokepoint is a function others call, not a block others copy.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise DataflowDefError(f"{where}: unreadable flow yaml: {e}") from e
    if not isinstance(raw, dict):
        raise DataflowDefError(f"{where}: flow yaml must be a mapping")
    try:
        return FlowDef.model_validate(raw)
    except Exception as e:
        raise DataflowDefError(f"{where}: invalid flow def: {e}") from e


def _load_def_file(path: Path, *, where: str) -> FlowDef:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise DataflowDefError(f"{where}: unreadable flow yaml: {e}") from e
    return parse_flow_def(text, where=where)


def load_flow_defs(*, strict: bool = False) -> dict[str, FlowDef]:
    """Every registered pack's flow defs keyed ``<pack>/<name>``, ALL kinds.

    Scans the unified ``flows/`` dir plus the legacy ``dataflows/`` alias
    (which may only hold ``kind: data``). A name collision across the two
    dirs is an error — the address must stay unambiguous.

    Lenient by default (a broken def logs + skips, matching the pack loader);
    ``strict=True`` raises with coordinates — the ``cairn validate`` path.
    """
    return {k: d for k, (d, _) in _scan_flow_defs(strict=strict).items()}


def load_flow_def_sources(*, strict: bool = False) -> dict[str, Path]:
    """The FILE each flow ref was loaded from, keyed identically to
    :func:`load_flow_defs`.

    A def's bytes are its identity: ``cairn flow export`` stamps the generated
    projection with ``sha256`` over this file, so a derivative always names the
    exact record it was cut from (ADR-0087 P4, #1256).
    """
    return {k: p for k, (_, p) in _scan_flow_defs(strict=strict).items()}


def flow_def_sha(path: Path) -> str:
    """``sha256:<hex>`` over a def file's bytes — same spelling ``cairn lock``
    uses for pack content, one file wide."""
    import hashlib  # noqa: PLC0415 - hashing is a leaf concern of this one helper

    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _scan_flow_defs(*, strict: bool = False) -> dict[str, tuple[FlowDef, Path]]:
    """The one scan; :func:`load_flow_defs` and :func:`load_flow_def_sources`
    are projections of it (two scans would be two chances to disagree)."""
    from cairn.core.pack.registry import get_packs  # noqa: PLC0415 - breaking a genuine import cycle (pack registry imports core models)

    defs: dict[str, tuple[FlowDef, Path]] = {}
    _UNRESOLVABLE.clear()  # #2069 — the record describes THIS load, not the last
    for pack in get_packs():
        sources: list[tuple[Path, bool]] = []
        fdir = getattr(pack, "flows_dir", None)
        if fdir and Path(fdir).is_dir():
            sources.append((Path(fdir), False))
        ddir = getattr(pack, "dataflows_dir", None)
        if ddir and Path(ddir).is_dir():
            sources.append((Path(ddir), True))
        for dirpath, legacy in sources:
            for f in sorted(dirpath.glob("*.yaml")):
                where = f"pack {pack.name!r} flow {f.stem!r} ({f})"
                key = f"{pack.name}/{f.stem}"
                try:
                    if key in defs:
                        raise DataflowDefError(
                            f"{where}: duplicate flow name — {key!r} is already "
                            "declared (flows/ and dataflows/ share one address space)"
                        )
                    d = _load_def_file(f, where=where)
                    if legacy and d.kind != FLOW_KIND_DATA:
                        raise DataflowDefError(
                            f"{where}: legacy dataflows/ only holds kind: data — "
                            f"a kind: {d.kind} flow belongs in flows/ (#1073)"
                        )
                    problems = validate_def(d, where=where)
                    if problems:
                        raise DataflowDefError("; ".join(problems))
                except DataflowDefError as e:
                    if strict:
                        raise
                    # #2069 — a def dropped here vanishes: every pipeline with a
                    # `flow:` leg pointing at it then fails with "flow X does not
                    # resolve", which names neither the stage nor the pack. Keep
                    # the reason so the launch door and the doctor can say what
                    # it cost. Wave D presented this as 19 unrelated-looking test
                    # failures for ONE missing entry point.
                    _UNRESOLVABLE[key] = str(e)
                    _log.warning(
                        "flow def %r is NOT loaded (every pipeline leg using it "
                        "will report 'does not resolve'): %s", key, e,
                    )
                    continue
                if d.name != f.stem:
                    _log.warning(
                        "%s: name %r != filename %r — the FILENAME addresses it",
                        where, d.name, f.stem,
                    )
                defs[key] = (d, f)
    return defs


def resolve_flow_key(ref: str) -> str:
    """A leg's flow ref → the KEY its def is registered under.

    Two normalizations, both of which existed as inline copies before #1792 and
    both of which now live in ONE place — :mod:`cairn.core.refs` (#2126):

    * the D0 kind prefix is stripped (a plan step records
      ``flow:<pack>/<name>``);
    * the pack DIR name (``dataset_readiness``) is reconciled with the
      MANIFEST name (``dataset-readiness``) — #1180 bug 6, where ``training``
      masked the split because it carries no hyphen.

    Returns the bare ``<pack>/<name>`` when nothing matches, so callers keep
    reporting the ref the author wrote and the "flow does not resolve" error
    names the thing the author actually typed.

    **A tie raises.** Two defs that fold onto one key would otherwise decide by
    registration order which FILE every ``flow:`` leg executes — the old
    three-spellings-in-order loop returned the first hit. That cannot be made to
    agree, so it fails loudly (#2126): a pack whose directory differs from a
    sibling's only by separator is a packaging bug, not a resolution one.
    """
    raw = bare_ref(ref)
    if "/" not in raw:
        return raw
    chosen, tied = pick(raw, load_flow_defs())
    if len(tied) > 1:
        raise DataflowDefError(
            f"flow ref {raw!r} names {len(tied)} installed flows — {tied!r}. "
            f"Their pack names differ only by `_` vs `-`, so they share one "
            f"address (#2126) and cairn will not guess which leg you meant: "
            f"rename one of the packs."
        )
    return chosen or raw


def flow_def_for(ref: str) -> Optional[FlowDef]:
    """The :class:`FlowDef` a leg ref names, or ``None``. One lookup — the leg
    executor, the bridge (kind + risk) and ``pipeline explain`` all read the
    flow through it, so "which file does this leg run" cannot differ between
    the thing that executes and the thing that describes."""
    return load_flow_defs().get(resolve_flow_key(ref))


#: A ``$config.<key>`` placeholder inside a stage's config block. The flow's
#: input surface IS the set of these names — a data/ml flow declares no schema
#: of its own, so the placeholders are the contract (see :func:`config_keys`).
_CONFIG_REF_RE = re.compile(r"\$config\.([A-Za-z_][A-Za-z0-9_]*)")


def config_keys(stages: Any) -> list[str]:
    """The ``$config.<key>`` names a flow's stage configs reference, in order.

    This IS a data/ml flow's declared input surface: what a ``flow:`` leg's
    ``with:`` fills in, what the flow editor offers as authorable passthrough
    fields, and — since #2134 (ADR-0106 D2) — what a wrapper pipeline adopts
    with ``inputs: {$inherit: <leg id>}``.

    It lives HERE, next to the model it reads, because three surfaces now ask
    the same question and a second copy of this regex would eventually answer
    it differently: the flow-document endpoint (``routers/builder``), the
    ``$inherit`` resolver (``engine/pipeline_inherit``), and anything that
    renders a flow's fields.
    """
    keys: list[str] = []
    seen: set[str] = set()
    for s in stages or []:
        for m in _CONFIG_REF_RE.finditer(str(getattr(s, "config", {}) or {})):
            if m.group(1) not in seen:
                seen.add(m.group(1))
                keys.append(m.group(1))
    return keys


def flow_config_keys(ref: str) -> Optional[list[str]]:
    """:func:`config_keys` for the flow a leg ref names; ``None`` when no such
    flow def resolves (the caller decides whether that is an error)."""
    d = flow_def_for(ref)
    return None if d is None else config_keys(d.stages)


def flow_leg_risk(ref: str) -> Optional[str]:
    """:func:`aggregate_risk` for the flow a leg ref names; ``None`` when the
    def cannot be read.

    THE risk the approval plane keys a flow leg on (#1760). It is a function
    rather than a block in the bridge because the bridge's copy partitioned the
    ref WITHOUT stripping the D0 ``flow:`` prefix — ``flow:training/sft-train``
    split into pack ``flow:training``, every lookup missed, and every flow leg
    reached :func:`~cairn.core.approval.enforce.enforce_flow_gate` with
    ``risk=None`` (which the shared table maps to the *medium* candidate). So a
    deployment policy naming the high-risk class governed nothing on the flow
    path — the #1402/#1688 shape again: the enforcement point was called, and
    fed a number that could never be right. ``None`` still resolves through the
    shared table; it is never treated as "no risk".
    """
    d = flow_def_for(ref)
    return aggregate_risk(d.stages) if d is not None else None


def load_dataflow_defs(*, strict: bool = False) -> dict[str, FlowDef]:
    """Back-compat #1051 surface: only the ``kind: data`` flows."""
    return {
        k: d for k, d in load_flow_defs(strict=strict).items()
        if d.kind == FLOW_KIND_DATA
    }


# ─── resolution (flow ref + $config substitution) ─────────────────────────


def _substitute(config_tmpl: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """``$config.<key>`` substitution via the ONE expression language (#1118).

    Missing key → the entry is OMITTED (resolve_value's ``omit`` mode), so the
    kernel's own default applies — the semantic this dialect always had, now a
    policy of the shared resolver instead of a fourth implementation.
    """
    return resolve_value(
        config_tmpl,
        Scopes(config=config),
        source="flow-def $config",
        on_missing="omit",
    )


def resolve_flow_ref(
    ref: str,
    config: Optional[dict[str, Any]] = None,
    *,
    kind: Optional[str] = None,
) -> list[StageRef]:
    """``<pack>/<name>`` → concrete stage list, ``$config.*`` substituted.

    ``kind`` pins the caller's door: a ``dataflow/run`` resolution passes
    ``data``, an ``ml/run`` resolution passes ``ml`` — a mismatch is an
    error, never a silent cross-kind run (ADR-0082 D7 kind-matched doors).
    Per-stage ``resources`` hints are preserved for the engine.
    """
    defs = load_flow_defs()
    d = defs.get(ref)
    if d is None:
        available = sorted(f"{k} (kind: {v.kind})" for k, v in defs.items())
        raise DataflowDefError(
            f"unknown flow ref {ref!r} — installed packs declare "
            f"{available or '(none)'}"
        )
    if kind is not None and d.kind != kind:
        raise DataflowDefError(
            f"flow {ref!r} is kind: {d.kind} — this door only runs kind: "
            f"{kind} flows (kind-matched doors, ADR-0082 D7)"
        )
    cfg = dict(config or {})
    return [
        StageRef(
            stage=s.stage,
            config=_substitute(dict(s.config), cfg),
            resources=s.resources,
            checkpoint=s.checkpoint,   # H6a (#1248) — declared durability rides through
            # #1379 — every DECLARED-behavior field must ride through, or the
            # resolved spec quietly means something the record didn't say.
            # `gate` is the fail-closed one (a dropped marker is a gate that
            # does not gate — the engine runner has no gate handling at all,
            # so it would execute straight past it); `map` (ADR-0090) would
            # silently collapse a declared fan-out into one un-mapped call.
            gate=s.gate,
            map=s.map,
        )
        for s in d.stages
    ]


def resolve_dataflow_ref(
    ref: str, config: Optional[dict[str, Any]] = None
) -> list[StageRef]:
    """Back-compat #1051 surface: resolve a ``kind: data`` flow."""
    return resolve_flow_ref(ref, config, kind=FLOW_KIND_DATA)
