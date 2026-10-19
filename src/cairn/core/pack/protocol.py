"""The Pack contract — what a domain pack contributes to the engine.

A pack is a plain data object: a name, a version, some filesystem asset
directories (templates / skills / policies), and lists of *descriptors* that
tell the engine how to build the pack's code contributions (tools, executors,
trigger adapters, memory stores, RAG sources, backends). It also carries its
own settings inventory and doctor checks so ``cairn doctor`` reports on the
pack's knobs alongside the engine's.

The engine never imports a pack module directly. It discovers packs via the
``cairn.packs`` entry-point group (see :mod:`cairn.core.pack.loader`),
registers them (see :mod:`cairn.core.pack.registry`), and consumes their
descriptors. This is the inversion that lets ``core/`` stay domain-neutral.

Descriptors carry *factories* rather than live instances so the engine controls
when (and whether) a contribution is constructed — a tool that needs an optional
dependency shouldn't blow up at discovery time, only when actually built.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional

if TYPE_CHECKING:  # avoid runtime import cycles — these are only type hints
    from cairn.core.pack.manifest import PackManifest
    from cairn.core.settings import SettingSpec


# ─── doctor result (part of the pack contract) ───────────────────────
#
# Packs contribute zero-or-more callables that return ``CheckResult``s;
# ``cairn doctor`` aggregates them. Lives here (not in cli/) so packs
# never depend on the CLI layer — only on the pack contract.

Status = Literal["ok", "warn", "fail"]


@dataclass
class CheckResult:
    """One row in the ``cairn doctor`` report.

    Packs author functions of shape ``() -> CheckResult`` and list them in
    :attr:`Pack.doctor_checks`. The CLI aggregator (``cli.commands.doctor``)
    runs them, groups by ``category``, and renders the report.
    """

    name: str
    status: Status
    detail: str = ""
    category: str = "settings"

    @property
    def symbol(self) -> str:
        return {"ok": "✓", "warn": "⚠", "fail": "✗"}[self.status]


@dataclass(frozen=True)
class ToolDescriptor:
    """One agent-callable capability the pack contributes.

    ``factory`` returns a ``langchain_core.tools.StructuredTool`` (or anything
    the compiler can bind into an agent_loop). ``name`` is the capability name
    templates request; ``category`` groups it for docs/UX (telemetry, topology,
    change, runbook, memory, postmortem, …).
    """

    name: str
    factory: Callable[[], object]
    category: str = "general"
    description: str = ""


@dataclass(frozen=True)
class CapabilityBinding:
    """A capability name bound to a concrete tool function + description.

    The shape every pack uses to declare one tool contribution. Carries the
    *bare callable* (``func``) — what the MCP gateway registers and what the
    template compiler wraps into a LangChain ``StructuredTool``. Lives in
    ``core/`` so packs and the engine seam can both reference it without
    pulling in ``services/``.

      - ``capability``   — template-facing name (``"read_logs"``).
      - ``func``         — coroutine doing the work.
      - ``name_for_llm`` — snake_case identifier shown to the LLM (and
                           registered on the MCP server, so it's also the
                           wire-level tool name).
      - ``description``  — what the tool does, surfaced to the LLM + MCP.
    """

    capability: str
    func: Callable[..., Any]
    description: str
    name_for_llm: str


@dataclass(frozen=True)
class ExecutorDescriptor:
    """An action executor the pack contributes (one per action_type)."""

    action_type: str
    factory: Callable[[], object]


@dataclass(frozen=True)
class TriggerAdapterDescriptor:
    """A webhook/trigger normalizer the pack contributes."""

    source: str           # e.g. "prometheus_alert"
    path: str             # FastAPI route, e.g. "/triggers/prometheus"
    factory: Callable[[], object]   # () -> APIRouter


@dataclass(frozen=True)
class BackendDescriptor:
    """An observability backend the pack can serve capabilities from."""

    name: str
    factory: Callable[[], object]


@dataclass(frozen=True)
class Pack:
    """A domain bundle the engine discovers and consumes.

    Required: ``name``, ``version``, ``description``. Everything else is an
    optional contribution. An empty pack (no tools, no templates) is valid —
    it just contributes nothing.
    """

    name: str
    version: str
    description: str

    # Discovery metadata — free-form tags (e.g. "domain:incident",
    # "data:logs", "runtime:langgraph"). Pure metadata, zero machinery: used
    # for listing/filtering/search, never for resolution or enforcement. A
    # future registry can index on these without any schema change here.
    tags: list[str] = field(default_factory=list)

    # Filesystem assets — absolute paths to the pack's asset directories.
    # Each field maps 1:1 to a key under [assets] in pack.toml. Optional: a
    # pack only sets the ones it ships. Engine consumption points read these
    # via the registry's `all_<kind>_dirs()` aggregators.
    #: #1125 — the pack's pipelines/ dir (ADR-0084 D4): its mandatory runnable
    #: surface, discovered and installed like templates.
    pipelines_dir: Optional[str] = None
    #: #1789 — the pack's presets/ dir (ADR-0071 §6): seed LAUNCH presets for the
    #: pipelines above, shipped as pure YAML data (packs are data, never code).
    presets_dir: Optional[str] = None
    #: #1133 — ONE concept: FLOW (ADR-0084 D2). `flows/` holds all three kinds:
    #: data/ml as `.yaml` (stage body) and agentic as `.md` (frontmatter + prose).
    #: `templates_dir` remains a working ALIAS during the transition; internal
    #: identifiers (`template_ref`) keep their names — the concept unifies now,
    #: the string churn is out of scope.
    flows_dir: Optional[str] = None
    templates_dir: Optional[str] = None
    prompts_dir: Optional[str] = None
    skills_dir: Optional[str] = None
    policies_dir: Optional[str] = None
    # Phase 1 additions — new asset kinds the engine knows how to enumerate.
    # See cairn/core/pack/manifest.py for the semantic intent of each.
    schemas_dir: Optional[str] = None
    triggers_dir: Optional[str] = None
    evals_dir: Optional[str] = None
    rag_dir: Optional[str] = None
    memory_dir: Optional[str] = None
    examples_dir: Optional[str] = None
    data_dir: Optional[str] = None
    dataflows_dir: Optional[str] = None  # ADR-0081 D2 (#1051): first-class dataflow defs

    # Code contributions, built lazily via their factories.
    tools: list[ToolDescriptor] = field(default_factory=list)
    executors: list[ExecutorDescriptor] = field(default_factory=list)
    triggers: list[TriggerAdapterDescriptor] = field(default_factory=list)
    backends: list[BackendDescriptor] = field(default_factory=list)

    # §Phase-2.8 — parsed ``triggers/*.yaml`` from the pack's
    # ``triggers_dir``. Populated by the loader on pack registration.
    # Each entry is a typed :class:`cairn.core.engine.triggers_spec.TriggerSpec`.
    # The runtime side (router) consumes these in Phase 2.9+; for now,
    # composition validation surfaces gaps + doctor renders them.
    trigger_specs: list[Any] = field(default_factory=list)
    # Bare-callable bindings (capability name → tool function). Same set of
    # tools as ``tools`` above, but exposing the underlying coroutine without
    # the StructuredTool wrapping — what the MCP gateway registers + what a
    # registry walker can list for discovery. Packs typically populate both
    # from the same source list (see ``tool_descriptors_from_bindings``).
    capability_bindings: list[CapabilityBinding] = field(default_factory=list)
    # Memory and RAG are core-provided services (see cairn.services.{memory,rag}).
    # A pack reaches them directly with its own schema; no descriptor needed.

    # Configuration + health.
    settings_inventory: list["SettingSpec"] = field(default_factory=list)
    doctor_checks: list[Callable[[], "CheckResult"]] = field(default_factory=list)

    # Declarative manifest (parsed ``pack.toml``), if the pack ships one.
    # Carries the data parts of the contract that don't need Python: name /
    # version / tags / asset paths / abstract service deps / runtime flags.
    # The Python ``Pack`` is still the canonical surface the engine consumes;
    # the manifest is just where pack authors *source* those values from.
    manifest: Optional["PackManifest"] = None

    @classmethod
    def from_manifest(
        cls,
        pkg_file: str,
        *,
        backends: Optional[list["BackendDescriptor"]] = None,
        doctor_checks: Optional[list[Callable[[], "CheckResult"]]] = None,
        tools: Optional[list["ToolDescriptor"]] = None,
        settings_inventory: Optional[list["SettingSpec"]] = None,
    ) -> "Pack":
        """Build the Pack straight from the ``pack.toml`` next to ``pkg_file``
        (#1539, ADR-0092 principle 1).

        Every first-party domain shim was 44–100 lines of the same wiring —
        ``find_manifest`` → ``load_manifest`` → one ``resolve_asset`` per
        declared dir → a copy-pasted flows-count doctor check — restating what
        the manifest already declares. A shim is now::

            from cairn.core.pack import Pack
            pack = Pack.from_manifest(__file__)

        plus keyword arguments for the ONLY things a manifest cannot carry:
        Python contributions (a backend factory, extra doctor checks, tools,
        settings). The standard flows-count check (#1391's shape) is built
        automatically whenever the manifest declares ``flows``; extra checks
        append after it.

        Raises loudly when ``pack.toml`` is missing or invalid — a pack that
        cannot state its manifest must not half-load (the #1511 lesson).
        """
        from pathlib import Path  # noqa: PLC0415 — stdlib, but keeps module import light

        from cairn.core.pack.manifest import find_manifest, load_manifest  # noqa: PLC0415 — avoid protocol↔manifest import cycle

        root = Path(pkg_file).resolve().parent
        manifest_path = find_manifest(root)
        if manifest_path is None:
            raise RuntimeError(f"pack at {root} is missing its pack.toml")
        m = load_manifest(manifest_path)  # ManifestError propagates — fail loud

        dirs: dict[str, Optional[str]] = {}
        for asset_key, pack_field in cls._ASSET_FIELDS.items():
            p = m.resolve_asset(root, asset_key)
            dirs[pack_field] = str(p) if p else None

        checks: list[Callable[[], CheckResult]] = []
        flows_dir = dirs.get("flows_dir")
        if flows_dir:
            checks.append(_standard_flows_check(m.name, Path(flows_dir)))
        checks.extend(doctor_checks or [])

        return cls(
            name=m.name,
            version=m.version,
            description=m.description,
            tags=list(m.tags),
            **dirs,
            backends=list(backends or []),
            tools=list(tools or []),
            settings_inventory=list(settings_inventory or []),
            doctor_checks=checks,
            manifest=m,
        )


# Manifest [assets] key → Pack field. Module-level (not a dataclass field) so
# dataclasses doesn't treat it as instance state; `templates` stays as the
# transition alias (#1133) and `dataflows` per ADR-0081 D2.
Pack._ASSET_FIELDS = {
    "flows": "flows_dir",
    "pipelines": "pipelines_dir",
    "presets": "presets_dir",
    "templates": "templates_dir",
    "skills": "skills_dir",
    "policies": "policies_dir",
    "schemas": "schemas_dir",
    "triggers": "triggers_dir",
    "evals": "evals_dir",
    "rag": "rag_dir",
    "examples": "examples_dir",
    "data": "data_dir",
    "dataflows": "dataflows_dir",
}


def _standard_flows_check(pack_name: str, flows_dir: "Any"):
    """The flows-count doctor check every first-party shim used to copy-paste
    (#1391's shape), written once (#1539). ok = flows present; warn = declared
    dir but no flows yet; fail = declared dir missing on disk."""
    def _check() -> CheckResult:
        if not flows_dir.is_dir():
            return CheckResult(
                f"{pack_name}_flows", "fail",
                f"flows dir missing: {flows_dir}", category=pack_name,
            )
        n = len(list(flows_dir.glob("*.md"))) + len(list(flows_dir.glob("*.yaml")))
        return CheckResult(
            f"{pack_name}_flows", "ok" if n else "warn",
            f"{n} flows bundled", category=pack_name,
        )
    return _check
