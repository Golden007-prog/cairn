"""Pack manifest — the declarative half of the Pack contract.

A ``pack.toml`` next to a pack's ``__init__.py`` carries the *data* parts of
the pack contract (name, version, tags, asset paths, abstract service deps,
runtime requirements). The Python ``Pack`` instance still owns the *code*
parts (tool / executor factories, doctor checks): those can't be expressed
in TOML.

Two reasons this split matters:

1. **Introspection without import.** A future ``cairn pack info <dir>``
   command — or a marketplace listing — can read the manifest without
   importing the pack's Python (which would run side effects and require
   optional deps).
2. **Single source of truth for declarative fields.** A pack reads its
   manifest at module-load time and feeds the values into its ``Pack(...)``
   constructor, so name/version/tags don't drift between TOML and Python.

The schema is **strict**: unknown top-level tables or unknown keys inside
known tables raise :class:`ManifestError`. This catches typos like
``[runtimes]`` early. The only optional table is everything except
``[pack]`` — a minimal manifest is just ``[pack]`` with ``name`` + ``version``.

§ Phase 1 (semantic ``[requires.*]`` migration): the legacy
``[requires].services = [...]`` list is replaced by typed sub-tables that
declare *what shape and access pattern the pack expects*, never *which
backend* — the deploying profile decides that. Sub-tables added:

  - ``[requires.mcp]``       — logical server names + tool allow-list
  - ``[requires.memory.<name>]``    — schema, access pattern, retention
  - ``[requires.artifacts.<name>]`` — content_type, retention
  - ``[requires.rag.<name>]``       — corpus path, access pattern, freshness

``[requires].services`` is removed with no back-compat shim (no community
packs exist yet; hard migration is cheaper than a deprecation cycle).
"""

from __future__ import annotations

from cairn.core.pack.contracts import validate_implements  # #2241 B — the reference grammar is this owner's

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from cairn.core.reporting import ReportSpec  # ADR-0103 — stdlib-only; the operator package would be a cycle


class ManifestError(ValueError):
    """A ``pack.toml`` failed to parse or violated the schema."""


# Closed schema: the only top-level tables we accept, and within each one,
# the only keys we accept. Unknown entries raise — typos are loud.
#
# The [assets] table covers every kind of pack-shipped artifact directory the
# engine knows how to enumerate. Each kind is independently optional: a pack
# only declares the ones it actually ships.
#
# §24.3 pack composition contract (P2.a):
#   - [provides]  — public surface a pack exports to other packs.
#   - [requires]  — gains typed sub-tables (mcp, memory.<n>, artifacts.<n>,
#                   rag.<n>) alongside ``capabilities``. Hard deps.
#   - [optional]  — soft deps.
#
# The [requires] table is *partially open*: top-level keys are closed
# (only the ones in _REQUIRES_TOP_KEYS), but `memory`, `artifacts`, `rag`
# are themselves tables whose sub-keys are user-chosen store names.
# Validation walks them with the per-kind allow-lists below.
_SCHEMA: dict[str, set[str]] = {
    # §Phase-2.1 — [pack] gains the locked tier/trust fields (kind,
    # publisher, trust_level) alongside the existing identity fields.
    # The legacy `type` key is also accepted (operator packs already
    # ship `type = "operator"`); Phase 2 leaves it as metadata only.
    "pack":     {
        "name", "version", "description", "tags",
        "type", "kind", "publisher", "trust_level",
    },
    # #495 (ADR-0061) — governance metadata for a knowledge pack's corpora.
    # Closed key set; all optional records-only in Slice 1 (per-corpus
    # overrides + policy enforcement land in later slices).
    "knowledge": {"license", "source", "citation_policy", "attribution"},
    "compat":   {"cairn_engine"},
    # #1106 E — `prompts` and `memory` REMOVED: declared in the schema, used by
    # zero packs, read by zero consumers. (`[requires.memory.<name>]` is a
    # different thing entirely — a real typed backend dep — and is untouched.)
    "assets":   {
        # #1124 — `pipelines/` is the pack's runnable surface (ADR-0084 D4).
        "pipelines",
        "templates", "skills", "policies",
        "schemas", "triggers", "evals", "rag", "examples", "data",
        "dataflows", "flows",
        # #2027 (ADR-0102 D5) — `models/` ships a curated model catalog as
        # data (YAML, no code): entries the launch dialog's picker reads.
        # A distinct key rather than reusing `data` because the catalog is
        # a first-class, schema-validated table (cairn/core/models_catalog.py
        # loads and validates it), not free-form pack data.
        "models",
        # #1789 — `presets/` ships SEED launch presets (ADR-0071 §6) as pure
        # YAML data: named input sets for the pack's own pipelines, so the
        # acceptance re-run of a 19-input flagship is `--preset <name>`.
        "presets",
    },
    "provides": {"templates", "capabilities", "policies",
                 "triggers", "schemas", "events"},
    "optional": {"capabilities"},
    "runtime":  {"temporal"},
    # `requires` is validated separately (see _validate_requires) because
    # its shape is partially open over sub-table names.
    # `operator` is also validated separately (see _validate_operators) —
    # the table key under [operator] is a user-chosen operator name like
    # "postmortem/write_file", not a fixed enum.
    # `demo` is also validated separately (see _validate_demos) — the table
    # key under [demo] is a user-chosen workflow (template) name.
}

# §394 — [demo."<template>"] declares a one-click "Try with sample data"
# for one workflow. Closed field set; ``inputs`` is an open map of prefill
# values keyed by the template's input-schema field names.
_DEMO_ENTRY_KEYS = {"title", "description", "inputs", "eval_path", "tier", "eval"}
# [demo."<wf>".eval] and [eval."<wf>"] — the ADR-0057 eval RECIPE: an operator
# ref (`uses`) + input bindings (`inputs`, whose values may be `$output.*`
# selectors, literals, or pack-relative golden paths). Closed key set.
_EVAL_RECIPE_KEYS = {"uses", "inputs"}
#: Keys allowed in an [eval."wf"] CONTRACT table (#487). Mutually exclusive
#: with the single-recipe form (a bare ``uses`` at the top → one gate).
_EVAL_CONTRACT_KEYS = {"gates", "objectives", "slice_by"}
#: Keys allowed in a [[eval."wf".objectives]] entry (#487).
_EVAL_OBJECTIVE_KEYS = {"name", "uses", "inputs", "floor"}

# [demo."<template>"].tier — coarse demo size class. Drives the tier picker
# in the Run form's "Try with sample data" control when a pack ships several.
_DEMO_TIER_VALUES = {"easy", "medium", "complex"}

# Top-level keys allowed under [requires]. `capabilities` is a list; the
# rest are tables with their own closed schemas below.
_REQUIRES_TOP_KEYS = {"capabilities", "mcp", "memory", "artifacts", "rag"}

# Closed schema for each typed sub-requirement.
_REQ_MCP_KEYS = {"servers", "tools"}
_REQ_MEMORY_KEYS = {"schema", "access", "retention", "embedding", "purpose"}
_REQ_ARTIFACT_KEYS = {"content_type", "retention"}
_REQ_RAG_KEYS = {"corpus", "access", "freshness", "role"}

# Allowed values for [runtime] capability flags. Forward-looking — doctor
# will eventually surface "required" entries the deploying profile didn't
# bind.
_RUNTIME_FLAG_VALUES = {"required", "optional"}


# ─── §Phase-2.1 enums ────────────────────────────────────────────────


# [pack].kind — what role this pack plays in the substrate.
#   - domain          → declarative workflow pack (templates / triggers /
#                       skills); contains no executable Python.
#   - operator        → Python operator pack: exposes typed Operations.
#   - service_provider→ operator pack that backs a substrate-level service
#                       (LLM, MCP, memory, artifact, RAG). Same packaging
#                       contract; substrate wires it under services.<kind>
#                       instead of bindings.<capability>.
_PACK_KIND_VALUES = {"domain", "operator", "service_provider", "knowledge"}

# [pack].trust_level — distribution / signing tier. No enforcement in
# Phase 2; doctor surfaces. Phase 3+ will gate production runs on this.
_TRUST_LEVEL_VALUES = {"builtin", "official", "community", "private", "local"}

# [operator."<n>"].risk — semantic risk class. Drives policy + approval
# routing later. Phase 2 records-only.
_OPERATOR_RISK_VALUES = {
    "read_only",            # observability reads, no side effects
    "external_write",       # writes to external SaaS (Slack post, GH comment, …)
    "production_mutation",  # touches user infra (k8s scale, DB write, rollback)
    "paid_compute",         # spins up paid GPUs / external compute
}

# [operator."<n>"].executors — how the operator can be dispatched.
# Locked enum per architecture memory § "Action lifecycle".
_OPERATOR_EXECUTOR_VALUES = {
    "in_process",         # direct Python call in the engine process
    "mcp_tool",           # routed through an MCP server
    "docker",             # containerised one-shot
    "kubernetes_job",     # k8s Job resource
    "temporal_activity",  # Temporal-backed activity
    "remote_job",         # provider-managed compute (RunPod, Lambda Cloud, …)
    "http_call",          # plain HTTP to a long-lived service
}

# Closed schema for the per-operator [operator."<n>"] table.
# Phase 2 backfills the existing 5 runtime flags (long_running, requires_gpu,
# supports_cancel/heartbeat/mock) alongside the new locked-architecture
# fields (risk, executors, requires_approval, supports_dry_run/verify/
# compensate).
_OPERATOR_ENTRY_KEYS = {
    # Identity
    "version",
    # Schemas — names of the input/output Pydantic models the runtime
    # finds in the operator pack's `schemas` module.
    "input_schema", "output_schema",
    # Runtime flags already shipped (consumed by the dispatcher today).
    "long_running", "requires_gpu",
    "supports_cancel", "supports_heartbeat", "supports_mock",
    # §Phase-2.1 locked-architecture additions. Phase 2 records-only;
    # Phase 3+ wires policy + executor dispatch.
    "risk", "executors",
    "requires_approval",
    "supports_dry_run", "supports_verify", "supports_compensate",
    # #2241 B (ADR-0111 D3/P4) — exact contract:// references, one capability ID.
    "implements",
}

# Closed schema for the [operator."<n>".capabilities] sub-sub-table.
_OPERATOR_ENTRY_CAPABILITIES_KEYS = {"provides"}

# Closed schema for the [operator."<n>".mcp] sub-sub-table (§24.3). An
# operator that declares the ``mcp_tool`` executor wraps exactly one MCP
# tool; this table names the logical server + tool the substrate MCP
# client dispatches to. The logical ``server`` is bound to a concrete
# transport by the profile (same logical-name model as [requires.mcp]).
_OPERATOR_ENTRY_MCP_KEYS = {"server", "tool"}

# Closed schema for the [operator."<n>".reports] sub-sub-table (ADR-0103).
# Mirrors the class's ``reports = ReportSpec(...)``; the manifest-sync gate
# compares the two. ``series`` is an array of ``{ name, unit }`` tables.
_OPERATOR_ENTRY_REPORTS_KEYS = {"stages", "series", "scalars"}


# ─── Typed sub-requirement records ───────────────────────────────────


@dataclass(frozen=True)
class McpRequirement:
    """A pack's MCP dependency — logical server names + tool allow-list.

    Logical names are pack-side identifiers; the profile binds each to a
    concrete MCP server (transport + URL/command). Tools are recorded as
    ``"<server>:<tool>"`` for audit and compile-time allow-list checks.
    """

    servers: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MemoryRequirement:
    """A named memory store the pack expects to read or write.

    ``schema`` is a relative path to the JSON Schema YAML (or a
    ``schema://`` URI) describing the store's record shape. ``access`` is
    the semantic access pattern (``similarity_search`` / ``exact_lookup`` /
    ``hybrid_search`` / …) — the profile picks a backend that satisfies it.
    ``retention`` is a duration string (e.g. ``"180d"``, ``"permanent"``).
    """

    schema: Optional[str] = None
    access: Optional[str] = None
    retention: Optional[str] = None
    embedding: Optional[dict[str, Any]] = None
    purpose: Optional[str] = None


@dataclass(frozen=True)
class ArtifactRequirement:
    """A named artifact kind the pack produces (RCA, report, dataset).

    ``content_type`` is the MIME type (e.g. ``"text/markdown"``).
    ``retention`` is a duration string.
    """

    content_type: Optional[str] = None
    retention: Optional[str] = None


@dataclass(frozen=True)
class RagRequirement:
    """A named RAG corpus the pack ships and expects indexed.

    ``corpus`` is a relative path under the pack root (typically a
    sub-directory of ``rag/``). ``access`` is the semantic access pattern
    (``hybrid_search`` / ``vector_search`` / ``bm25``). ``freshness`` is
    when the index should refresh (``on_change`` / ``daily`` / …).
    """

    corpus: Optional[str] = None
    access: Optional[str] = None
    freshness: Optional[str] = None
    #: #496 (ADR-0061) — the GLOBAL knowledge role this corpus provides into.
    #: Any workflow whose template declares this role slot composes this corpus
    #: (union across every installed pack providing the same role). ``None`` =
    #: the corpus is queried only by an explicit named node (the legacy path).
    role: Optional[str] = None


@dataclass(frozen=True)
class KnowledgeMeta:
    """Governance metadata for a ``kind="knowledge"`` pack's corpora (#495,
    ADR-0061). Records-only in Slice 1 — surfaced by doctor/registry and
    (Slice 4) enforced by policy at context-injection time.

    ``license`` = the corpus's distribution license (e.g. ``"CC-BY-4.0"``);
    ``source`` = provenance URL/citation of the upstream data;
    ``citation_policy`` = how a workflow must cite hits from this corpus;
    ``attribution`` = the human-facing credit line.
    """

    license: Optional[str] = None
    source: Optional[str] = None
    citation_policy: Optional[str] = None
    attribution: Optional[str] = None


@dataclass(frozen=True)
class OperatorEntry:
    """One operator exposed by an operator pack.

    Parsed from a ``[operator."<pack>/<op-name>"]`` table in ``pack.toml``.
    Operator packs declare one entry per Operation they ship. Phase 2
    records-only — the runtime dispatcher reads these via the entry-point
    group (`cairn.operators`); fields here are surfaced by doctor and
    will gate policy + executor selection in Phase 3+.

    The ``risk`` and ``executors`` fields are validated against the locked
    enums (see :data:`_OPERATOR_RISK_VALUES`, :data:`_OPERATOR_EXECUTOR_VALUES`).
    The five lifecycle support flags (``supports_dry_run`` / ``supports_verify``
    / ``supports_compensate``) join the three older ones (``supports_cancel``
    / ``supports_heartbeat`` / ``supports_mock``) on the same dataclass —
    they all describe optional capability of the underlying Operation.
    """

    #: The fully-qualified operator name (``"<pack>/<op-name>"``), pulled
    #: from the TOML table key.
    name: str
    #: Pack-author version of the operator. Distinct from ``[pack].version``
    #: so an operator can iterate without bumping the whole pack.
    version: Optional[str] = None
    input_schema: Optional[str] = None
    output_schema: Optional[str] = None

    # ── runtime flags already shipped (existing Operation surface) ──
    long_running: bool = False
    requires_gpu: bool = False
    supports_cancel: bool = False
    supports_heartbeat: bool = False
    supports_mock: bool = False

    # ── §Phase-2.1 additions ──
    risk: Optional[str] = None
    executors: list[str] = field(default_factory=list)
    requires_approval: bool = False
    supports_dry_run: bool = False
    supports_verify: bool = False
    supports_compensate: bool = False

    # ── [operator."<n>".capabilities] sub-sub-table ──
    provides_capabilities: list[str] = field(default_factory=list)
    #: #2241 B — exact ``contract://<id>/<rev>`` references (empty = legacy).
    implements: list[str] = field(default_factory=list)

    # ── [operator."<n>".mcp] sub-sub-table (§24.3) ──
    #: Logical MCP server name (profile binds it to a transport). Required
    #: when ``mcp_tool`` is in ``executors``; ``None`` otherwise.
    mcp_server: Optional[str] = None
    #: Tool name on that server the operator wraps.
    mcp_tool: Optional[str] = None

    # ── [operator."<n>".reports] sub-sub-table (ADR-0103) ──
    #: What the operator reports (stages · series · scalars). ``None`` here
    #: means the table is absent, which the sync gate treats as
    #: ``ReportSpec.NONE`` — the class must still state it explicitly.
    reports: Optional[ReportSpec] = None


@dataclass(frozen=True)
class EvalRecipe:
    """A declarative eval recipe (ADR-0057, epic #442).

    The same shape as a demo seed — an operator ref plus input bindings —
    but wired to a run's *output* rather than the workflow's input. Two homes:

      - ``[demo."<wf>".eval]`` — TEST eval: golden, deterministic (``eval/subset``),
        run in CI + ``cairn eval`` to prove the pack still behaves as tested.
      - ``[eval."<wf>"]`` — RUNTIME eval: reference-free (``model/gate`` /
        ``eval/judge`` / ``eval/benchmark``), scored on real runs' output.

    ``inputs`` values may be ``$output.<dotted.path>`` selectors (bound to the
    run's output by the scoring-pass dispatcher — see
    :func:`cairn.core.eval.recipe.run_eval_recipe`), plain literals, or
    pack-relative golden-file paths (rewritten to absolute by :meth:`resolved`).

    The recipe is **declarative-only** (guardrail): a ``uses`` operator ref +
    input bindings. Custom scoring MUST be a real operator, never an inline
    snippet — the pack declares what "good" looks like; an operator computes it.
    """

    #: The eval operator ref, e.g. ``"eval/subset"`` — resolves through the same
    #: registry/composition path as workflow operators (D2: a capability, not an enum).
    uses: str
    #: Input bindings for the eval operator's schema. ``$output.*`` selectors are
    #: left verbatim (resolved at scoring time); file paths are resolved by
    #: :meth:`resolved`.
    inputs: dict[str, Any] = field(default_factory=dict)

    def resolved(self, root: Path) -> "EvalRecipe":
        """Rewrite pack-relative golden paths in ``inputs`` against ``root``.

        Only values naming an on-disk file under the pack are rewritten;
        ``$output.*`` selectors, HF ids, and other scalars pass through
        untouched (shares :meth:`DemoSpec._resolve_one`)."""
        new_inputs = {k: DemoSpec._resolve_one(v, root) for k, v in self.inputs.items()}
        return EvalRecipe(uses=self.uses, inputs=new_inputs)


@dataclass(frozen=True)
class NamedObjective:
    """A soft *quality* objective within an :class:`EvalContract` (#487).

    Same declarative shape as an :class:`EvalRecipe` (a ``uses`` operator ref +
    input bindings) plus a human ``name`` and an optional ``floor`` — a run
    scoring below the floor is ``degraded``. **Parsed here, SCORED later**: a
    quality signal is only safe to act on once the metric is trustworthy, so
    objectives stay unscored until #458 lands (ADR-0060 D6 / Phase 6). Declaring
    them now keeps the manifest shape stable — turning scoring on is a one-line
    flip, not a schema change."""

    #: Human label for the objective (e.g. ``"faithfulness"``), surfaced in the
    #: report + used as the eval-case ``case_id`` once scored.
    name: str
    #: The eval recipe that computes the objective's score.
    recipe: EvalRecipe
    #: Optional score floor below which the run is ``degraded`` (scored @ #458).
    floor: Optional[float] = None

    def resolved(self, root: Path) -> "NamedObjective":
        return NamedObjective(name=self.name, recipe=self.recipe.resolved(root), floor=self.floor)


@dataclass(frozen=True)
class EvalContract:
    """A workflow's declared definition of "good" (#487, ADR-0060 D2).

    Three facets, all optional:
      - ``gates`` — HARD, blocking eval recipes. Any gate returning ``fail``
        makes the run's aggregate verdict ``fail`` (``warn`` → ``degraded``).
        Deterministic; UNGATED (human-inspectable).
      - ``objectives`` — SOFT quality signals (:class:`NamedObjective`). Parsed
        here, scored only after #458 (see that class's docstring).
      - ``slice_by`` — run-property dimension names (dotted paths into the run's
        output/inputs). Each auto-eval tags its case results with these values
        so Insights can answer "failures cluster on ``dataset=synthetic``".

    The legacy single-recipe ``[eval."wf"]`` form — a bare ``uses``/``inputs``
    table — parses as a **one-gate contract** with no objectives or slices, so
    every existing pack is unaffected (full back-compat)."""

    gates: tuple[EvalRecipe, ...] = ()
    objectives: tuple[NamedObjective, ...] = ()
    slice_by: tuple[str, ...] = ()

    @property
    def primary_gate(self) -> Optional[EvalRecipe]:
        """The first hard gate, or ``None`` — the back-compat single-recipe view
        that :func:`~cairn.core.eval.recipe.resolve_runtime_recipe` and the
        one-shot ``POST /runs/{id}/evaluate`` path project."""
        return self.gates[0] if self.gates else None

    def resolved(self, root: Path) -> "EvalContract":
        return EvalContract(
            gates=tuple(g.resolved(root) for g in self.gates),
            objectives=tuple(o.resolved(root) for o in self.objectives),
            slice_by=self.slice_by,
        )


@dataclass(frozen=True)
class DemoSpec:
    """A one-click demo for a single workflow (§394).

    Parsed from a ``[demo."<template>"]`` table in ``pack.toml``. Every field
    is optional so a bare ``[demo."<template>"]`` (or just ``inputs``) is legal
    and packs without any ``[demo]`` table are unaffected (full back-compat).

    ``inputs`` is a map of prefill values keyed by the workflow's input-schema
    field names — the "Try with sample data" button drops these into the Run
    form. Relative example paths inside ``inputs`` (and ``eval_path``) are
    resolved against the pack root by :meth:`resolved`, so a pack can ship
    ``path = "examples/sample.jsonl"`` and have it point at the real file, or a
    HuggingFace Hub dataset id (``"databricks/databricks-dolly-15k"``) that
    ``_io.load_rows`` consumes natively — the latter is left untouched (no such
    file exists under the pack, so it is not path-rewritten).
    """

    #: The workflow (template) name this demo prefills, from the TOML table key.
    workflow: str
    title: str = ""
    description: str = ""
    #: Prefill values for the Run form, keyed by input-schema field name.
    inputs: dict[str, Any] = field(default_factory=dict)
    #: Optional relative/absolute path (or HF id) to an eval set for the demo.
    eval_path: Optional[str] = None
    #: One of ``_DEMO_TIER_VALUES`` when declared; ``None`` = untiered.
    tier: Optional[str] = None
    #: ADR-0057 TEST-eval recipe attached to this demo tier — golden,
    #: deterministic scoring of the tier's output. ``None`` = no recipe.
    eval: Optional["EvalRecipe"] = None

    @staticmethod
    def _resolve_one(val: Any, root: Path) -> Any:
        """Resolve a single prefill value against ``root`` when it names a file
        that actually exists under the pack; otherwise return it unchanged.

        This keeps HF Hub ids (``"org/dataset"``) and plain scalars intact — we
        only rewrite a string when ``root / val`` is a real path on disk."""
        if isinstance(val, str) and val:
            candidate = (root / val)
            try:
                if candidate.exists():
                    return str(candidate.resolve())
            except OSError:
                return val
        return val

    def resolved(self, root: Path) -> "DemoSpec":
        """Return a copy with relative example paths resolved against ``root``.

        Only values that name an on-disk file under the pack are rewritten;
        HF Hub ids and other scalars pass through untouched (see
        :meth:`_resolve_one`)."""
        new_inputs = {k: self._resolve_one(v, root) for k, v in self.inputs.items()}
        new_eval = self._resolve_one(self.eval_path, root) if self.eval_path else None
        return DemoSpec(
            workflow=self.workflow,
            title=self.title,
            description=self.description,
            inputs=new_inputs,
            eval_path=new_eval,
            tier=self.tier,
            eval=self.eval.resolved(root) if self.eval else None,
        )


@dataclass(frozen=True)
class PackManifest:
    """One pack's declarative metadata, parsed from ``pack.toml``.

    Asset paths are stored as **relative strings** here (as written in TOML).
    Callers resolve them against the pack root when constructing the ``Pack``;
    keeping them relative keeps the manifest itself location-independent.
    """

    # [pack] — required
    name: str
    version: str
    description: str = ""
    tags: list[str] = field(default_factory=list)

    # §Phase-2.1 — tier / trust / role metadata. Phase 2 records-only;
    # Phase 3+ wires policy (signature checks, production gates, service
    # provider wiring).
    #
    # ``pack_type`` shadows the TOML key ``type`` (Python keyword conflict).
    # Today's values are "operator" / "domain" / None (legacy domain packs
    # don't declare it). Phase 2 keeps it open metadata.
    pack_type: Optional[str] = None
    #: One of ``_PACK_KIND_VALUES``. Default ``None`` = legacy domain pack.
    #: Phase 2: domain manifests typically leave unset; operator manifests
    #: set ``"operator"``; service-provider packs (Phase 3+) set
    #: ``"service_provider"``.
    kind: Optional[str] = None
    #: Free-form publisher identifier (``"cairn"``, ``"community/<user>"``,
    #: ``"private/<org>"``). Surfaced by doctor; gated on in Phase 3+.
    publisher: Optional[str] = None
    #: One of ``_TRUST_LEVEL_VALUES``. Controls distribution + signing
    #: policy in Phase 3+.
    trust_level: Optional[str] = None
    #: [knowledge] governance metadata (#495, ADR-0061). Set on a
    #: ``kind="knowledge"`` pack; ``None`` for every other kind.
    knowledge: Optional["KnowledgeMeta"] = None

    # [compat] — a PEP 440 range for the engine this pack was written against.
    # #1545: the loader checks it against the installed `cairn` distribution
    # (``loader.engine_compat_status``) and WARNS on a mismatch (log + doctor
    # row); it never rejects. Was parse-only from introduction until then.
    cairn_engine: Optional[str] = None

    # [assets] — relative paths; None when the pack doesn't ship that asset kind.
    templates: Optional[str] = None
    skills: Optional[str] = None
    policies: Optional[str] = None
    schemas: Optional[str] = None
    triggers: Optional[str] = None
    evals: Optional[str] = None
    rag: Optional[str] = None
    examples: Optional[str] = None
    data: Optional[str] = None
    dataflows: Optional[str] = None  # ADR-0081 D2 (#1051): first-class dataflow defs
    flows: Optional[str] = None  # ADR-0082 D7 (#1073): unified flows/ kind (data|ml)
    models: Optional[str] = None  # #2027 (ADR-0102 D5): curated model catalog data

    # pipelines/ — the pack's top orchestrator(s) (#1124, ADR-0084 D4).
    pipelines: Optional[str] = None

    # presets/ — seed launch presets for those pipelines (#1789, ADR-0071 §6).
    presets: Optional[str] = None

    # [requires.capabilities] — hard capability deps. Names that some
    # installed pack must `provides.capabilities`.
    required_capabilities: list[str] = field(default_factory=list)

    # [requires.mcp] — logical MCP server names + tool allow-list.
    # ``None`` when the pack declares no MCP dependency.
    required_mcp: Optional[McpRequirement] = None

    # [requires.memory.<name>] — typed memory stores by name.
    required_memory: dict[str, MemoryRequirement] = field(default_factory=dict)

    # [requires.artifacts.<name>] — typed artifact kinds by name.
    required_artifacts: dict[str, ArtifactRequirement] = field(default_factory=dict)

    # [requires.rag.<name>] — typed RAG corpora by name.
    required_rag: dict[str, RagRequirement] = field(default_factory=dict)

    # [optional] — soft capability deps.
    optional_capabilities: list[str] = field(default_factory=list)

    # [provides] — §24.3 public surface. The presence of the section toggles
    # encapsulation: a pack with no [provides] behaves permissively (today's
    # everything-visible default); a pack with [provides] is encapsulated and
    # only the listed names are visible to other packs.
    provides_declared: bool = False
    provides_templates: list[str] = field(default_factory=list)
    provides_capabilities: list[str] = field(default_factory=list)
    provides_policies: list[str] = field(default_factory=list)
    provides_triggers: list[str] = field(default_factory=list)
    provides_schemas: list[str] = field(default_factory=list)
    provides_events: list[str] = field(default_factory=list)

    # [runtime] — capability flags ("required" | "optional"). Forward-looking.
    runtime_flags: dict[str, str] = field(default_factory=dict)

    # §Phase-2.1 — [operator."<n>"] tables, one entry per operator the
    # pack exposes. Keyed by the fully-qualified operator name
    # ("<pack>/<op-name>"). Empty for domain packs.
    operators: dict[str, "OperatorEntry"] = field(default_factory=dict)

    # §394 — [demo."<template>"] tables, one (or, for tiered demos via the TOML
    # array-of-tables form, several) per demoable workflow. Keyed by the
    # template name (e.g. "finetune") → the ordered list of its demo tiers.
    # Empty when the pack ships no demo.
    demos: dict[str, list["DemoSpec"]] = field(default_factory=dict)

    # ADR-0057 (#442) + #487 — top-level [eval."<workflow>"] RUNTIME eval
    # CONTRACTS, keyed by template name → the reference-free gates/objectives/
    # slices scored on real runs' output. Empty when the pack ships no runtime
    # eval. A legacy single-recipe [eval."wf"] table parses as a one-gate
    # contract (back-compat). (TEST-eval recipes live on each DemoSpec.eval.)
    runtime_evals: dict[str, "EvalContract"] = field(default_factory=dict)

    # ─── filesystem-resolved view ───────────────────────────────────────

    def resolve_asset(self, root: Path, kind: str) -> Optional[Path]:
        """Resolve one of the asset kinds against the pack root.

        Returns ``None`` when the manifest didn't declare that asset kind.
        Does not check existence — that's a job for ``doctor``.
        """
        rel = getattr(self, kind, None)
        if not rel:
            return None
        return (root / rel).resolve()


# ─── parsing ─────────────────────────────────────────────────────────


def find_manifest(pkg_dir: Path) -> Optional[Path]:
    """Return the path to a ``pack.toml`` in ``pkg_dir``, or ``None``."""
    candidate = pkg_dir / "pack.toml"
    return candidate if candidate.is_file() else None


def load_manifest(path: Path) -> PackManifest:
    """Parse ``pack.toml`` and validate against the closed schema.

    Raises :class:`ManifestError` on any structural problem: missing required
    fields, unknown tables, unknown keys within a known table, wrong types,
    or out-of-range enum values. The message always names the offending key
    so authors can fix it without rereading the schema.
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(f"{path}: invalid TOML — {e}") from e
    except OSError as e:
        raise ManifestError(f"{path}: cannot read — {e}") from e

    _check_unknown_tables(raw, path)

    pack_tbl = _required_table(raw, "pack", path)
    _check_unknown_keys("pack", pack_tbl, path)

    name = _required_str(pack_tbl, "pack.name", path)
    version = _required_str(pack_tbl, "pack.version", path)
    description = _optional_str(pack_tbl, "pack.description", path, default="")
    tags = _optional_str_list(pack_tbl, "pack.tags", path)

    # §Phase-2.1 — locked tier/trust fields. All open metadata at parse
    # time; the enum-bounded ones (kind, trust_level) are validated against
    # the locked sets.
    pack_type = _optional_str(pack_tbl, "pack.type", path, default=None)
    kind = _optional_enum_str(
        pack_tbl, "pack.kind", path,
        allowed=_PACK_KIND_VALUES, default=None,
    )
    publisher = _optional_str(pack_tbl, "pack.publisher", path, default=None)
    trust_level = _optional_enum_str(
        pack_tbl, "pack.trust_level", path,
        allowed=_TRUST_LEVEL_VALUES, default=None,
    )

    # #495 (ADR-0061) — [knowledge] governance metadata (records-only in Slice 1).
    knowledge_tbl = raw.get("knowledge")
    knowledge: Optional[KnowledgeMeta] = None
    if knowledge_tbl is not None:
        _check_unknown_keys("knowledge", knowledge_tbl, path)
        knowledge = KnowledgeMeta(
            license=_optional_str(knowledge_tbl, "knowledge.license", path, default=None),
            source=_optional_str(knowledge_tbl, "knowledge.source", path, default=None),
            citation_policy=_optional_str(knowledge_tbl, "knowledge.citation_policy", path, default=None),
            attribution=_optional_str(knowledge_tbl, "knowledge.attribution", path, default=None),
        )

    compat_tbl = raw.get("compat", {}) or {}
    _check_unknown_keys("compat", compat_tbl, path)
    cairn_engine = _optional_str(compat_tbl, "compat.cairn_engine", path,
                                    default=None)

    assets_tbl = raw.get("assets", {}) or {}
    _check_unknown_keys("assets", assets_tbl, path)

    def _asset(kind: str) -> Optional[str]:
        """Declared path, else the standard dir when it EXISTS (#1106 D).

        All seven shipped packs carry an `[assets]` block that mostly restates
        the obvious (`templates = "templates"`). Convention removes the
        ceremony; `[assets]` is reserved for genuine exceptions.

        An explicit entry ALWAYS wins — including when it names a missing dir,
        because falling back would mask a typo by silently loading a different
        directory than the author named. Only directories are discovered: a
        stray file called `templates` is not an asset root.
        """
        declared = _optional_str(assets_tbl, f"assets.{kind}", path, default=None)
        if declared is not None:
            return declared
        return kind if (path.parent / kind).is_dir() else None

    pipelines = _asset("pipelines")
    presets = _asset("presets")
    templates = _asset("templates")
    skills = _asset("skills")
    policies = _asset("policies")
    schemas = _asset("schemas")
    triggers = _asset("triggers")
    evals = _asset("evals")
    rag = _asset("rag")
    examples = _asset("examples")
    data = _asset("data")
    dataflows = _asset("dataflows")
    flows = _asset("flows")
    models = _asset("models")

    requires_tbl = raw.get("requires", {}) or {}
    (
        required_capabilities,
        required_mcp,
        required_memory,
        required_artifacts,
        required_rag,
    ) = _validate_requires(requires_tbl, path)

    optional_tbl = raw.get("optional", {}) or {}
    _check_unknown_keys("optional", optional_tbl, path)
    optional_capabilities = _optional_str_list(
        optional_tbl, "optional.capabilities", path,
    )

    # The *presence* of [provides] toggles encapsulation, even if every
    # field inside is empty (an "I export nothing" pack is legal — and
    # still encapsulated). `raw.get(...)` returning None vs an empty dict
    # would muddle that, so we key off membership.
    provides_declared = "provides" in raw
    provides_tbl = raw.get("provides", {}) or {}
    _check_unknown_keys("provides", provides_tbl, path)
    provides_templates = _optional_str_list(
        provides_tbl, "provides.templates", path,
    )
    provides_capabilities = _optional_str_list(
        provides_tbl, "provides.capabilities", path,
    )
    provides_policies = _optional_str_list(
        provides_tbl, "provides.policies", path,
    )
    provides_triggers = _optional_str_list(
        provides_tbl, "provides.triggers", path,
    )
    provides_schemas = _optional_str_list(
        provides_tbl, "provides.schemas", path,
    )
    provides_events = _optional_str_list(
        provides_tbl, "provides.events", path,
    )

    runtime_tbl = raw.get("runtime", {}) or {}
    _check_unknown_keys("runtime", runtime_tbl, path)
    runtime_flags: dict[str, str] = {}
    for cap, val in runtime_tbl.items():
        if not isinstance(val, str) or val not in _RUNTIME_FLAG_VALUES:
            raise ManifestError(
                f"{path}: runtime.{cap} must be one of "
                f"{sorted(_RUNTIME_FLAG_VALUES)!r}, got {val!r}"
            )
        runtime_flags[cap] = val

    # §Phase-2.1 — [operator."<n>"] tables. Open over operator names,
    # closed over each entry's field set.
    operators = _validate_operators(raw.get("operator"), path)

    # §394 — [demo."<template>"] tables. Open over workflow names, closed
    # over each entry's field set.
    demos = _validate_demos(raw.get("demo"), path)
    runtime_evals = _validate_runtime_evals(raw.get("eval"), path)

    # #495 (ADR-0061) — a knowledge pack's whole point is its corpora; a
    # corpus-only pack (no templates/operators) is valid, but an EMPTY one is
    # not — reject it so the mistake surfaces at load, not at an empty search.
    if kind == "knowledge" and not required_rag:
        raise ManifestError(
            f'{path}: a kind="knowledge" pack must declare at least one '
            "[requires.rag.<name>] corpus (it ships governed knowledge, not automation)."
        )

    return PackManifest(
        pipelines=pipelines,
        presets=presets,
        name=name,
        version=version,
        description=description,
        tags=tags,
        pack_type=pack_type,
        kind=kind,
        publisher=publisher,
        trust_level=trust_level,
        knowledge=knowledge,
        cairn_engine=cairn_engine,
        templates=templates,
        skills=skills,
        policies=policies,
        schemas=schemas,
        triggers=triggers,
        evals=evals,
        rag=rag,
        examples=examples,
        data=data,
        dataflows=dataflows,
        flows=flows,
        models=models,
        required_capabilities=required_capabilities,
        required_mcp=required_mcp,
        required_memory=required_memory,
        required_artifacts=required_artifacts,
        required_rag=required_rag,
        optional_capabilities=optional_capabilities,
        provides_declared=provides_declared,
        provides_templates=provides_templates,
        provides_capabilities=provides_capabilities,
        provides_policies=provides_policies,
        provides_triggers=provides_triggers,
        provides_schemas=provides_schemas,
        provides_events=provides_events,
        runtime_flags=runtime_flags,
        operators=operators,
        demos=demos,
        runtime_evals=runtime_evals,
    )


# ─── helpers: validation primitives ─────────────────────────────────


def _valid_specifier(spec: str) -> bool:
    s = (spec or "").strip()
    if s in ("", "*"):
        return True
    from packaging.specifiers import InvalidSpecifier, SpecifierSet  # noqa: PLC0415 - base dep

    try:
        SpecifierSet(s if any(c in s for c in "<>=!~") else f"=={s}")
    except InvalidSpecifier:
        return False
    return True


def version_satisfies(installed: str, spec: str) -> bool:
    """Does ``installed`` satisfy the ``[uses]`` specifier ``spec``? (#1104)

    ``""`` / ``"*"`` = no constraint. A bare ``"0.2"`` means exactly 0.2 (the
    shorthand an author reaches for). NEVER raises: an unparseable installed
    version reports unsatisfied — loud, but not a crash inside doctor.
    """
    s = (spec or "").strip()
    if s in ("", "*"):
        return True
    from packaging.specifiers import InvalidSpecifier, SpecifierSet  # noqa: PLC0415 - base dep
    from packaging.version import InvalidVersion, Version  # noqa: PLC0415 - base dep

    try:
        ss = SpecifierSet(s if any(c in s for c in "<>=!~") else f"=={s}")
        return Version(str(installed).strip()) in ss
    except (InvalidSpecifier, InvalidVersion, TypeError):
        return False


def _check_unknown_tables(raw: dict[str, Any], path: Path) -> None:
    # `requires`, `operator` and `demo` are allowed but validated separately
    # (open over sub-table names), so add them to the closed set for the
    # top-level check.
    allowed = set(_SCHEMA.keys()) | {"requires", "operator", "demo", "eval"}
    bad = sorted(set(raw.keys()) - allowed)
    if bad:
        raise ManifestError(
            f"{path}: unknown top-level table(s): {bad!r}. "
            f"Allowed: {sorted(allowed)!r}."
        )


def _check_unknown_keys(table_name: str, tbl: dict[str, Any], path: Path) -> None:
    # The [runtime] table is open over capability names but closed over value
    # shape (validated separately). Every other table in _SCHEMA is closed.
    if table_name == "runtime":
        return
    allowed = _SCHEMA[table_name]
    bad = sorted(set(tbl.keys()) - allowed)
    if bad:
        raise ManifestError(
            f"{path}: unknown key(s) in [{table_name}]: {bad!r}. "
            f"Allowed: {sorted(allowed)!r}."
        )


def _required_table(raw: dict[str, Any], name: str, path: Path) -> dict[str, Any]:
    tbl = raw.get(name)
    if not isinstance(tbl, dict):
        raise ManifestError(f"{path}: missing required table [{name}]")
    return tbl


def _required_str(tbl: dict[str, Any], dotted: str, path: Path) -> str:
    # Take the *last* component as the leaf key so this helper works for
    # both 2-component (``pack.name``) and N-component (``requires.mcp
    # .servers``) dotted paths. The full ``dotted`` string is only used in
    # error messages.
    key = dotted.rsplit(".", 1)[1]
    if key not in tbl:
        raise ManifestError(f"{path}: missing required key {dotted!r}")
    val = tbl[key]
    if not isinstance(val, str) or not val:
        raise ManifestError(f"{path}: {dotted!r} must be a non-empty string")
    return val


def _optional_str(
    tbl: dict[str, Any], dotted: str, path: Path, *, default: Optional[str],
) -> Optional[str]:
    key = dotted.rsplit(".", 1)[1]
    if key not in tbl:
        return default
    val = tbl[key]
    if val is None:
        return default
    if not isinstance(val, str):
        raise ManifestError(f"{path}: {dotted!r} must be a string")
    return val


def _optional_str_list(tbl: dict[str, Any], dotted: str, path: Path) -> list[str]:
    key = dotted.rsplit(".", 1)[1]
    if key not in tbl:
        return []
    val = tbl[key]
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        raise ManifestError(f"{path}: {dotted!r} must be a list of strings")
    return list(val)


# ─── helpers: [requires.*] sub-tables ────────────────────────────────


def _validate_requires(
    tbl: dict[str, Any],
    path: Path,
) -> tuple[
    list[str],
    Optional[McpRequirement],
    dict[str, MemoryRequirement],
    dict[str, ArtifactRequirement],
    dict[str, RagRequirement],
]:
    """Walk the [requires.*] tree and return parsed records.

    The top-level keys are closed (only `capabilities`, `mcp`, `memory`,
    `artifacts`, `rag` are accepted) but the sub-tables under `memory`,
    `artifacts`, and `rag` are open over user-chosen store names. The legacy
    ``[requires].services`` key is rejected — packs migrate to the typed
    sub-tables.
    """
    bad = sorted(set(tbl.keys()) - _REQUIRES_TOP_KEYS)
    if bad:
        # Helpful error for the common legacy case
        hint = ""
        if "services" in bad:
            hint = (
                " — `services` was replaced by typed sub-tables; "
                "use [requires.memory.<name>], [requires.rag.<name>], etc."
            )
        raise ManifestError(
            f"{path}: unknown key(s) in [requires]: {bad!r}. "
            f"Allowed: {sorted(_REQUIRES_TOP_KEYS)!r}.{hint}"
        )

    required_capabilities = _optional_str_list(
        tbl, "requires.capabilities", path,
    )

    mcp_tbl = tbl.get("mcp")
    required_mcp: Optional[McpRequirement] = None
    if mcp_tbl is not None:
        if not isinstance(mcp_tbl, dict):
            raise ManifestError(
                f"{path}: [requires.mcp] must be a table"
            )
        bad_mcp = sorted(set(mcp_tbl.keys()) - _REQ_MCP_KEYS)
        if bad_mcp:
            raise ManifestError(
                f"{path}: unknown key(s) in [requires.mcp]: {bad_mcp!r}. "
                f"Allowed: {sorted(_REQ_MCP_KEYS)!r}."
            )
        servers = _optional_str_list(
            mcp_tbl, "requires.mcp.servers", path,
        )
        tools = _optional_str_list(
            mcp_tbl, "requires.mcp.tools", path,
        )
        required_mcp = McpRequirement(servers=servers, tools=tools)

    required_memory = _parse_memory_subtable(tbl.get("memory"), path)
    required_artifacts = _parse_artifacts_subtable(tbl.get("artifacts"), path)
    required_rag = _parse_rag_subtable(tbl.get("rag"), path)

    return (
        required_capabilities,
        required_mcp,
        required_memory,
        required_artifacts,
        required_rag,
    )


def _parse_memory_subtable(
    raw: Any,
    path: Path,
) -> dict[str, MemoryRequirement]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: [requires.memory] must be a table")
    out: dict[str, MemoryRequirement] = {}
    for store_name, sub in raw.items():
        if not isinstance(sub, dict):
            raise ManifestError(
                f"{path}: [requires.memory.{store_name}] must be a table"
            )
        bad = sorted(set(sub.keys()) - _REQ_MEMORY_KEYS)
        if bad:
            raise ManifestError(
                f"{path}: unknown key(s) in [requires.memory.{store_name}]: "
                f"{bad!r}. Allowed: {sorted(_REQ_MEMORY_KEYS)!r}."
            )
        embedding = sub.get("embedding")
        if embedding is not None and not isinstance(embedding, dict):
            raise ManifestError(
                f"{path}: [requires.memory.{store_name}].embedding "
                f"must be a table"
            )
        out[store_name] = MemoryRequirement(
            schema=_optional_str(
                sub, f"requires.memory.{store_name}.schema", path, default=None,
            ),
            access=_optional_str(
                sub, f"requires.memory.{store_name}.access", path, default=None,
            ),
            retention=_optional_str(
                sub, f"requires.memory.{store_name}.retention", path, default=None,
            ),
            embedding=embedding,
            purpose=_optional_str(
                sub, f"requires.memory.{store_name}.purpose", path, default=None,
            ),
        )
    return out


def _parse_artifacts_subtable(
    raw: Any,
    path: Path,
) -> dict[str, ArtifactRequirement]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: [requires.artifacts] must be a table")
    out: dict[str, ArtifactRequirement] = {}
    for artifact_name, sub in raw.items():
        if not isinstance(sub, dict):
            raise ManifestError(
                f"{path}: [requires.artifacts.{artifact_name}] must be a table"
            )
        bad = sorted(set(sub.keys()) - _REQ_ARTIFACT_KEYS)
        if bad:
            raise ManifestError(
                f"{path}: unknown key(s) in [requires.artifacts.{artifact_name}]: "
                f"{bad!r}. Allowed: {sorted(_REQ_ARTIFACT_KEYS)!r}."
            )
        out[artifact_name] = ArtifactRequirement(
            content_type=_optional_str(
                sub, f"requires.artifacts.{artifact_name}.content_type", path,
                default=None,
            ),
            retention=_optional_str(
                sub, f"requires.artifacts.{artifact_name}.retention", path,
                default=None,
            ),
        )
    return out


def _parse_rag_subtable(
    raw: Any,
    path: Path,
) -> dict[str, RagRequirement]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: [requires.rag] must be a table")
    out: dict[str, RagRequirement] = {}
    for corpus_name, sub in raw.items():
        if not isinstance(sub, dict):
            raise ManifestError(
                f"{path}: [requires.rag.{corpus_name}] must be a table"
            )
        bad = sorted(set(sub.keys()) - _REQ_RAG_KEYS)
        if bad:
            raise ManifestError(
                f"{path}: unknown key(s) in [requires.rag.{corpus_name}]: "
                f"{bad!r}. Allowed: {sorted(_REQ_RAG_KEYS)!r}."
            )
        out[corpus_name] = RagRequirement(
            corpus=_optional_str(
                sub, f"requires.rag.{corpus_name}.corpus", path, default=None,
            ),
            access=_optional_str(
                sub, f"requires.rag.{corpus_name}.access", path, default=None,
            ),
            freshness=_optional_str(
                sub, f"requires.rag.{corpus_name}.freshness", path, default=None,
            ),
            role=_optional_str(
                sub, f"requires.rag.{corpus_name}.role", path, default=None,
            ),
        )
    return out


# ─── §Phase-2.1 helpers ──────────────────────────────────────────────


def _optional_enum_str(
    tbl: dict[str, Any],
    dotted: str,
    path: Path,
    *,
    allowed: set[str],
    default: Optional[str],
) -> Optional[str]:
    """Like ``_optional_str`` but rejects values outside ``allowed``.

    The error names both the offending value and the allowed set so the
    pack author doesn't have to grep for the enum.
    """
    val = _optional_str(tbl, dotted, path, default=default)
    if val is not None and val not in allowed:
        raise ManifestError(
            f"{path}: {dotted!r} must be one of {sorted(allowed)!r}, "
            f"got {val!r}"
        )
    return val


def _optional_bool(
    tbl: dict[str, Any], dotted: str, path: Path, *, default: bool,
) -> bool:
    key = dotted.rsplit(".", 1)[1]
    if key not in tbl:
        return default
    val = tbl[key]
    if not isinstance(val, bool):
        raise ManifestError(f"{path}: {dotted!r} must be a boolean")
    return val


def _validate_operators(
    raw: Any,
    path: Path,
) -> dict[str, OperatorEntry]:
    """Walk the [operator."<n>"] tree and return parsed entries.

    The top level under [operator] is open over user-chosen operator
    names (typically the fully-qualified ``<pack>/<op-name>`` form).
    Each entry's field set is closed against :data:`_OPERATOR_ENTRY_KEYS`
    plus the special ``capabilities`` sub-sub-table.

    Phase 2 is records-only — the runtime dispatcher still reads through
    the entry-point group, not these tables. Validation here keeps the
    declared metadata honest so Phase 3+ can enforce against it.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: [operator] must be a table of tables")
    out: dict[str, OperatorEntry] = {}
    for op_name, sub in raw.items():
        if not isinstance(sub, dict):
            raise ManifestError(
                f"{path}: [operator.{op_name!r}] must be a table"
            )
        # `capabilities`, `mcp` and `reports` are sub-sub-tables; everything
        # else must be in the closed key set.
        flat_keys = {k for k in sub.keys() if k not in ("capabilities", "mcp", "reports")}
        bad = sorted(flat_keys - _OPERATOR_ENTRY_KEYS)
        if bad:
            raise ManifestError(
                f"{path}: unknown key(s) in [operator.{op_name!r}]: "
                f"{bad!r}. Allowed: {sorted(_OPERATOR_ENTRY_KEYS)!r}."
            )

        executors = _optional_str_list(
            sub, f"operator.{op_name}.executors", path,
        )
        for ex in executors:
            if ex not in _OPERATOR_EXECUTOR_VALUES:
                raise ManifestError(
                    f"{path}: operator.{op_name!r}.executors entry {ex!r} "
                    f"is not a known executor; "
                    f"allowed: {sorted(_OPERATOR_EXECUTOR_VALUES)!r}"
                )

        risk = _optional_enum_str(
            sub, f"operator.{op_name}.risk", path,
            allowed=_OPERATOR_RISK_VALUES, default=None,
        )

        # [operator."<n>".capabilities].provides
        caps_tbl = sub.get("capabilities")
        if caps_tbl is None:
            provides_caps: list[str] = []
        elif not isinstance(caps_tbl, dict):
            raise ManifestError(
                f"{path}: [operator.{op_name!r}.capabilities] must be a table"
            )
        else:
            cap_bad = sorted(
                set(caps_tbl.keys()) - _OPERATOR_ENTRY_CAPABILITIES_KEYS,
            )
            if cap_bad:
                raise ManifestError(
                    f"{path}: unknown key(s) in "
                    f"[operator.{op_name!r}.capabilities]: {cap_bad!r}. "
                    f"Allowed: {sorted(_OPERATOR_ENTRY_CAPABILITIES_KEYS)!r}."
                )
            provides_caps = _optional_str_list(
                caps_tbl,
                f"operator.{op_name}.capabilities.provides",
                path,
            )

        # [operator."<n>".mcp].server / .tool (§24.3)
        try:
            implements = list(validate_implements(
                _optional_str_list(sub, f"operator.{op_name}.implements", path)))
        except ValueError as e:
            raise ManifestError(f"{path}: operator.{op_name!r}.implements: {e}") from e
        mcp_tbl = sub.get("mcp")
        mcp_server: Optional[str] = None
        mcp_tool: Optional[str] = None
        if mcp_tbl is not None:
            if not isinstance(mcp_tbl, dict):
                raise ManifestError(
                    f"{path}: [operator.{op_name!r}.mcp] must be a table"
                )
            mcp_bad = sorted(set(mcp_tbl.keys()) - _OPERATOR_ENTRY_MCP_KEYS)
            if mcp_bad:
                raise ManifestError(
                    f"{path}: unknown key(s) in [operator.{op_name!r}.mcp]: "
                    f"{mcp_bad!r}. Allowed: {sorted(_OPERATOR_ENTRY_MCP_KEYS)!r}."
                )
            mcp_server = _optional_str(
                mcp_tbl, f"operator.{op_name}.mcp.server", path, default=None,
            )
            mcp_tool = _optional_str(
                mcp_tbl, f"operator.{op_name}.mcp.tool", path, default=None,
            )

        # Loud failure: an operator whose ONLY way to run is mcp_tool must
        # name the tool it wraps, or it's unrunnable. Operators that also
        # list ``in_process`` (e.g. clickstack's backend-abstracted queries)
        # are exempt — in_process is the fallback, and the runtime guard in
        # ``invoke_operator_mcp_tool`` still fails loud if mcp_tool is ever
        # actually picked without a mapping.
        if (
            "mcp_tool" in executors
            and "in_process" not in executors
            and not (mcp_server and mcp_tool)
        ):
            raise ManifestError(
                f"{path}: operator {op_name!r} can only run via 'mcp_tool' but "
                f"is missing [operator.{op_name!r}.mcp] server/tool — both are "
                f"required so the dispatcher knows which MCP tool to call. "
                f"(Add an in_process executor as a fallback, or declare the mcp "
                f"mapping.)"
            )

        # ADR-0103 — the reporting declaration.
        rep_tbl = sub.get("reports")
        reports: Optional[ReportSpec] = None
        if rep_tbl is not None:
            if not isinstance(rep_tbl, dict):
                raise ManifestError(
                    f"{path}: [operator.{op_name!r}.reports] must be a table"
                )
            rep_bad = sorted(set(rep_tbl.keys()) - _OPERATOR_ENTRY_REPORTS_KEYS)
            if rep_bad:
                raise ManifestError(
                    f"{path}: unknown key(s) in [operator.{op_name!r}.reports]: "
                    f"{rep_bad!r}. Allowed: {sorted(_OPERATOR_ENTRY_REPORTS_KEYS)!r}."
                )
            for key in ("stages", "scalars"):
                val = rep_tbl.get(key)
                if val is not None and (not isinstance(val, list) or not all(isinstance(x, str) for x in val)):
                    raise ManifestError(
                        f"{path}: [operator.{op_name!r}.reports].{key} must be a list of strings"
                    )
            ser = rep_tbl.get("series")
            if ser is not None and (
                not isinstance(ser, list)
                or not all(isinstance(x, dict) and isinstance(x.get("name"), str) for x in ser)
            ):
                raise ManifestError(
                    f"{path}: [operator.{op_name!r}.reports].series must be a list of "
                    f"{{ name = \"…\", unit = \"…\" }} tables"
                )
            try:
                spec = ReportSpec.from_manifest(rep_tbl)
            except ValueError as e:
                raise ManifestError(f"{path}: [operator.{op_name!r}.reports]: {e}") from e
            reports = None if spec.is_none else spec

        out[op_name] = OperatorEntry(
            name=op_name,
            version=_optional_str(
                sub, f"operator.{op_name}.version", path, default=None,
            ),
            input_schema=_optional_str(
                sub, f"operator.{op_name}.input_schema", path, default=None,
            ),
            output_schema=_optional_str(
                sub, f"operator.{op_name}.output_schema", path, default=None,
            ),
            long_running=_optional_bool(
                sub, f"operator.{op_name}.long_running", path, default=False,
            ),
            requires_gpu=_optional_bool(
                sub, f"operator.{op_name}.requires_gpu", path, default=False,
            ),
            supports_cancel=_optional_bool(
                sub, f"operator.{op_name}.supports_cancel", path, default=False,
            ),
            supports_heartbeat=_optional_bool(
                sub, f"operator.{op_name}.supports_heartbeat", path, default=False,
            ),
            supports_mock=_optional_bool(
                sub, f"operator.{op_name}.supports_mock", path, default=False,
            ),
            risk=risk,
            executors=executors,
            requires_approval=_optional_bool(
                sub, f"operator.{op_name}.requires_approval", path, default=False,
            ),
            supports_dry_run=_optional_bool(
                sub, f"operator.{op_name}.supports_dry_run", path, default=False,
            ),
            supports_verify=_optional_bool(
                sub, f"operator.{op_name}.supports_verify", path, default=False,
            ),
            supports_compensate=_optional_bool(
                sub, f"operator.{op_name}.supports_compensate", path, default=False,
            ),
            provides_capabilities=provides_caps,
            implements=implements,
            mcp_server=mcp_server,
            mcp_tool=mcp_tool,
            reports=reports,
        )
    return out


# ─── §394 helpers: [demo."<template>"] sub-tables ────────────────────


def _parse_one_demo(wf_name: str, sub: Any, path: Path) -> DemoSpec:
    """Parse a single ``[demo."<template>"]`` (or one array-of-tables entry)
    into a :class:`DemoSpec`. Closed over :data:`_DEMO_ENTRY_KEYS`."""
    if not isinstance(sub, dict):
        raise ManifestError(f"{path}: [demo.{wf_name!r}] must be a table")
    bad = sorted(set(sub.keys()) - _DEMO_ENTRY_KEYS)
    if bad:
        raise ManifestError(
            f"{path}: unknown key(s) in [demo.{wf_name!r}]: {bad!r}. "
            f"Allowed: {sorted(_DEMO_ENTRY_KEYS)!r}."
        )
    inputs = sub.get("inputs", {})
    if not isinstance(inputs, dict):
        raise ManifestError(
            f"{path}: [demo.{wf_name!r}].inputs must be a table"
        )
    tier = _optional_enum_str(
        sub, f"demo.{wf_name}.tier", path,
        allowed=_DEMO_TIER_VALUES, default=None,
    )
    eval_sub = sub.get("eval")
    recipe = (
        _parse_eval_recipe(f"demo.{wf_name}.eval", eval_sub, path)
        if eval_sub is not None
        else None
    )
    return DemoSpec(
        workflow=wf_name,
        title=_optional_str(sub, f"demo.{wf_name}.title", path, default="") or "",
        description=_optional_str(
            sub, f"demo.{wf_name}.description", path, default="",
        ) or "",
        inputs=dict(inputs),
        eval_path=_optional_str(
            sub, f"demo.{wf_name}.eval_path", path, default=None,
        ),
        tier=tier,
        eval=recipe,
    )


def _parse_eval_recipe(where: str, sub: Any, path: Path) -> EvalRecipe:
    """Parse an eval recipe table (``[demo.*.eval]`` or ``[eval.*]``) into an
    :class:`EvalRecipe`. Closed over :data:`_EVAL_RECIPE_KEYS` (ADR-0057)."""
    if not isinstance(sub, dict):
        raise ManifestError(f"{path}: [{where}] must be a table")
    bad = sorted(set(sub.keys()) - _EVAL_RECIPE_KEYS)
    if bad:
        raise ManifestError(
            f"{path}: unknown key(s) in [{where}]: {bad!r}. "
            f"Allowed: {sorted(_EVAL_RECIPE_KEYS)!r}."
        )
    uses = _required_str(sub, f"{where}.uses", path)
    inputs = sub.get("inputs", {})
    if not isinstance(inputs, dict):
        raise ManifestError(f"{path}: [{where}].inputs must be a table")
    return EvalRecipe(uses=uses, inputs=dict(inputs))


def _as_table_list(raw: Any, where: str, path: Path) -> list[dict]:
    """Normalize a TOML array-of-tables value → ``list[dict]`` (``None`` → [])."""
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(x, dict) for x in raw):
        raise ManifestError(f"{path}: [[{where}]] must be an array of tables")
    return raw


def _parse_named_objective(where: str, sub: Any, path: Path) -> NamedObjective:
    """Parse one ``[[eval."wf".objectives]]`` entry (#487): a named recipe with
    an optional score ``floor``. Closed over :data:`_EVAL_OBJECTIVE_KEYS`."""
    if not isinstance(sub, dict):
        raise ManifestError(f"{path}: [{where}] must be a table")
    bad = sorted(set(sub.keys()) - _EVAL_OBJECTIVE_KEYS)
    if bad:
        raise ManifestError(
            f"{path}: unknown key(s) in [{where}]: {bad!r}. "
            f"Allowed: {sorted(_EVAL_OBJECTIVE_KEYS)!r}."
        )
    name = _required_str(sub, f"{where}.name", path)
    uses = _required_str(sub, f"{where}.uses", path)
    inputs = sub.get("inputs", {})
    if not isinstance(inputs, dict):
        raise ManifestError(f"{path}: [{where}].inputs must be a table")
    floor = sub.get("floor")
    if floor is not None and not isinstance(floor, (int, float)):
        raise ManifestError(f"{path}: [{where}].floor must be a number")
    return NamedObjective(
        name=name,
        recipe=EvalRecipe(uses=uses, inputs=dict(inputs)),
        floor=float(floor) if floor is not None else None,
    )


def _parse_eval_contract(where: str, sub: Any, path: Path) -> EvalContract:
    """Parse an ``[eval."wf"]`` table into an :class:`EvalContract` (#487).

    Two accepted forms:
      - legacy single-recipe (a bare ``uses`` at top) → a one-gate contract;
      - contract (``gates`` / ``objectives`` / ``slice_by``) — array-of-tables
        for the recipe lists, a string list for the slice dimensions."""
    if not isinstance(sub, dict):
        raise ManifestError(f"{path}: [{where}] must be a table")
    # Back-compat: a top-level `uses` means the single-recipe form → one gate.
    if "uses" in sub:
        return EvalContract(gates=(_parse_eval_recipe(where, sub, path),))
    bad = sorted(set(sub.keys()) - _EVAL_CONTRACT_KEYS)
    if bad:
        raise ManifestError(
            f"{path}: unknown key(s) in [{where}]: {bad!r}. Allowed: "
            f"{sorted(_EVAL_CONTRACT_KEYS)!r} (or a single-recipe `uses`/`inputs`)."
        )
    gates = tuple(
        _parse_eval_recipe(f"{where}.gates[{i}]", g, path)
        for i, g in enumerate(_as_table_list(sub.get("gates"), f"{where}.gates", path))
    )
    objectives = tuple(
        _parse_named_objective(f"{where}.objectives[{i}]", o, path)
        for i, o in enumerate(_as_table_list(sub.get("objectives"), f"{where}.objectives", path))
    )
    slice_by = sub.get("slice_by", [])
    if not isinstance(slice_by, list) or not all(isinstance(s, str) for s in slice_by):
        raise ManifestError(f"{path}: [{where}].slice_by must be a list of strings")
    return EvalContract(gates=gates, objectives=objectives, slice_by=tuple(slice_by))


def _validate_runtime_evals(raw: Any, path: Path) -> dict[str, "EvalContract"]:
    """Walk the top-level ``[eval."<workflow>"]`` tables → runtime eval CONTRACTS.

    Open over workflow (template) names; each value is one :class:`EvalContract`
    (gates + objectives + slices, or a legacy single-recipe one-gate form).
    Fully optional — ``None`` yields ``{}`` and packs without an ``[eval]``
    table are unaffected (full back-compat)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: [eval] must be a table of tables")
    out: dict[str, EvalContract] = {}
    for wf_name, sub in raw.items():
        out[wf_name] = _parse_eval_contract(f"eval.{wf_name}", sub, path)
    return out


def _validate_demos(
    raw: Any,
    path: Path,
) -> dict[str, list[DemoSpec]]:
    """Walk the [demo."<template>"] tree and return parsed demo specs.

    The top level under [demo] is open over user-chosen workflow (template)
    names; each entry's field set is closed against :data:`_DEMO_ENTRY_KEYS`.
    ``inputs`` is an open map of prefill values (any JSON scalar/collection);
    ``tier``, when present, is validated against :data:`_DEMO_TIER_VALUES`.

    A workflow maps to a **list** of demos: a single ``[demo."<template>"]``
    table yields one entry, while the TOML array-of-tables form
    (``[[demo."<template>"]]``) yields one per tier. Fully optional — ``None``
    (no ``[demo]`` table) yields ``{}`` and packs without a demo are unaffected.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: [demo] must be a table of tables")
    out: dict[str, list[DemoSpec]] = {}
    for wf_name, sub in raw.items():
        if isinstance(sub, list):
            out[wf_name] = [_parse_one_demo(wf_name, e, path) for e in sub]
        else:
            out[wf_name] = [_parse_one_demo(wf_name, sub, path)]
    return out
