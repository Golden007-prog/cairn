"""Operation Protocol + OperationContext (§24.20.12, §24.20.13).

This module defines the contract operator authors implement. The runtime
side (binding resolution, routing, OperationContext binding to host services)
lives in other modules — OP3 (operation step type), OP4 (binding evaluator),
OP6 (routing), §24.14 C3/C6/C7 (cost/artifact/storage), §24.15 O1 (metrics).

Per the §24.20 lock spec, this Protocol mirrors the shape verified by
/verify-upstream against the action ``Executor`` (then ``cairn.core.actions``; the
platform's since #2497).

  - Function-shaped capabilities (:class:`CapabilityRegistry`) keep their
    place for ``tool_call`` step type — lightweight inline calls.
  - Class-shaped operators (this file) own the ``operation`` step type —
    typed I/O, optional external execution.

The two coexist by design; neither replaces the other.
"""

from __future__ import annotations


import inspect
import logging
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Generic,
    Optional,
    Protocol,
    TypeVar,
    runtime_checkable,
)

from pydantic import BaseModel

from cairn.core.operator.reports import Reporter, ReportSpec  # ADR-0103 — no cycle: reports imports nothing from here

if TYPE_CHECKING:
    # type-only: keep protocol.py decoupled from the connections module at runtime
    from cairn.core.connections import ResolvedConnection

I = TypeVar("I", bound=BaseModel)
O = TypeVar("O", bound=BaseModel)


# ─── ResourceSpec (§24.20.5 — resources block) ────────────────────────────


@dataclass(frozen=True)
class ResourceSpec:
    """Resource requirements declared by an operator class.

    Domain pack operation bindings can OVERRIDE these per-call (e.g.,
    a binding sets ``timeout_minutes: 60`` even though the operator
    declares ``timeout_minutes: 240``). The compiler enforces that
    binding overrides do not RELAX a constraint the operator considers
    a minimum (e.g., ``min_gpu_memory_gb``).
    """

    cpu: Optional[int] = None
    memory_gb: Optional[int] = None
    gpu_count: Optional[int] = None
    gpu_type: Optional[str] = None
    disk_gb: Optional[int] = None
    timeout_minutes: Optional[int] = None


# ─── Store protocols (§24.20.13) ──────────────────────────────────────────
#
# These are runtime services injected into OperationContext. Real
# implementations land in §24.14 C7 (object storage), §24.15 O1 (OTel),
# §24.14 C3 (cost ledger), OP10 (SecretStore). For OP1 substrate, Null
# impls are sufficient — they let inline operators run end-to-end without
# any of those substrates landing.


@runtime_checkable
class ArtifactStore(Protocol):
    """Operator-side artifact handle (§24.14 C7)."""

    def put(self, name: str, ref: str) -> str:
        """Record an artifact reference; return its canonical URI."""
        ...

    def get(self, name: str) -> Optional[str]:
        """Look up a previously-put artifact ref by name."""
        ...


@runtime_checkable
class SecretStore(Protocol):
    """Scoped secret accessor (§24.20.13, OP10).

    Only keys explicitly declared in the operation binding's ``secrets:``
    block are readable. ``get()`` raises ``KeyError`` for undeclared keys.
    """

    def get(self, name: str) -> str: ...

    def available_keys(self) -> list[str]: ...


@runtime_checkable
class MetricsLogger(Protocol):
    """DEPRECATED (ADR-0103, #2035): the untyped ``ctx.metrics.log(key, value)``
    surface. Use ``ctx.report.series(step, name=value)`` / ``.scalars(...)``,
    which validate names against the class's ``reports`` declaration. Kept
    for one release so existing operators keep running; removed after."""
    """Operator-side metric emitter (§24.15 O1)."""

    def log(self, key: str, value: Any) -> None: ...


@runtime_checkable
class CostLedger(Protocol):
    """Operator-side cost charge (§24.14 C3)."""

    def charge(self, usd: float, reason: str = "") -> None: ...

    def consumed(self) -> float:
        """Total USD consumed so far on this operation."""
        ...


# ─── Null implementations (default for OP1 substrate) ─────────────────────


class NullArtifactStore:
    """No-op artifact store — returns the ref unchanged.

    Used when no §24.14 C7 backend is bound. Suitable for inprocess
    runs that don't need durable artifact tracking.
    """

    def __init__(self) -> None:
        self._refs: dict[str, str] = {}

    def put(self, name: str, ref: str) -> str:
        self._refs[name] = ref
        return ref

    def get(self, name: str) -> Optional[str]:
        return self._refs.get(name)


class NullSecretStore:
    """Empty secret store — every ``get`` raises ``KeyError``.

    Operators that declare ``secrets: [HF_TOKEN]`` in their binding and
    receive a ``NullSecretStore`` will fail loud at runtime — the
    expected behavior when no SecretStore backend is bound.
    """

    def get(self, name: str) -> str:
        raise KeyError(
            f"secret {name!r} not available — no SecretStore bound. "
            "Bind one via profile (PRF1 #194) or OperationContext."
        )

    def available_keys(self) -> list[str]:
        return []


class NullMetricsLogger:
    """No-op metrics logger — discards everything."""

    def log(self, key: str, value: Any) -> None:
        return None


class NullCostLedger:
    """No-op cost ledger — accepts charges, reports 0."""

    def __init__(self) -> None:
        self._total: float = 0.0

    def charge(self, usd: float, reason: str = "") -> None:
        self._total += float(usd)

    def consumed(self) -> float:
        return self._total


# ─── OperationContext (§24.20.13) ─────────────────────────────────────────


#: ADR-0120 D7 (#2455) — ``await ctx.launch(ref, inputs, trigger)`` → the run or
#: pipeline id. Bound by the dispatch layer as a closure over the launch door.
LaunchHandle = Callable[[str, dict[str, Any], dict[str, Any]], Awaitable[str]]


@dataclass
class OperationContext:
    """Runtime context handed to :meth:`Operation.run`.

    Strictly bounded surface per §24.20.13:

      - Identity (pipeline_id, node_name, is_mocked).
      - Service handles (artifacts, secrets, metrics, cost, logger) and the
        deployment's store handles (``run_store()``, ``schedule_store()``,
        ``fingerprint_store()`` — ADR-0120 D7 / #2453).
      - Long-running helpers (emit_heartbeat, check_cancelled) — only
        meaningful when the operator declares ``supports_heartbeat``
        or ``supports_cancel``.

    Notably absent (LOCKED OUT per §24.20.13):

      - Reading other nodes' outputs (resolved at binding time).
      - Reading graph state outside ``inp``.
      - Modifying the graph.
      - Spawning other operators — except starting *governed* work through
        ``launch`` (ADR-0120 D7), which goes through the launch door like any
        other launch.
      - Direct runtime access (Temporal, K8s).

    The binding YAML resolves all reads at compile time; only the resolved
    value reaches the operator. This is the boundary that keeps replay
    honest.
    """

    pipeline_id: str
    node_name: str
    is_mocked: bool = False

    artifacts: ArtifactStore = field(default_factory=NullArtifactStore)
    secrets: SecretStore = field(default_factory=NullSecretStore)
    metrics: MetricsLogger = field(default_factory=NullMetricsLogger)
    cost: CostLedger = field(default_factory=NullCostLedger)

    # #121: the resolved compute connection (config + secrets) when the binding
    # declares ``execution.compute_target`` — set at dispatch for remote-job
    # operators so ``submit``/``poll`` can reach the chosen backend (RunPod,
    # Vast.ai, …). ``None`` for in-process operators. A deployment-resolved
    # handle (resolved from the binding, like ``secrets``) — not graph access.
    compute: "Optional[ResolvedConnection]" = None

    # ADR-0103 (#2035) — the typed reporter: the ONLY way an operator emits
    # progress / metrics. Validates every name against the class's ``reports``
    # declaration; the runtime binds it to the run's event store. The default
    # is a null reporter with an empty declaration, so an operator that was
    # never given one drops-with-a-warning rather than crashing.
    report: "Reporter" = field(default_factory=lambda: Reporter.null())

    # ADR-0118 D4 — for a ``dataflow/run``/``ml/run`` node of an admitted template leg:
    # the leg stamp plus that node's admitted stage contracts by stage index
    # (``{"leg", "error", "stages": {<index>: {stage, contract, schemas}}}``), which the
    # door copies into the spec it submits. ``None`` outside an admitted leg (legacy).
    dataflow_contracts: Optional[dict[str, Any]] = None

    # ADR-0120 D7 (#2455) — the ONE sanctioned way an operator starts other governed
    # work: ``await ctx.launch(ref, inputs, trigger)`` returns the run or pipeline id.
    # The dispatch layer binds it as a closure over the launch door, so admission,
    # policy gates and budget apply exactly as for any launch and only a declared
    # pipeline runs (ADR-0106 D1). ``None`` outside a governed run (dev loop, tests):
    # the operator reports that it could not launch and never builds a dispatcher.
    launch: Optional[LaunchHandle] = None

    # ADR-0120 D7 / #2453 (group B, owner decision 2026-10-12) — the deployment's STORE
    # handles reach an operator the way ``secrets`` and ``compute`` do: through the
    # context, never through the storage package. ``None`` = the deployment default,
    # resolved on first use by the accessor below (an operator that never asks opens
    # nothing); a test or the dispatch layer injects its own. Handles the context
    # opened itself are closed by :meth:`release_handles` after ``run`` (the runtime
    # calls it); an injected handle is never closed here.
    runs: Optional[Any] = None          # the run store: records and their event log
    schedules: Optional[Any] = None     # the schedule store: a watch's own state row
    fingerprints: Optional[Any] = None  # the dataset fingerprint index
    _opened: list = field(default_factory=list, repr=False)

    def run_store(self) -> Any:
        """The run store (process-wide; not closed by this context)."""
        if self.runs is None:
            from cairn.core.storage import get_storage  # noqa: PLC0415 — lazy: the storage stack stays off the operator import path

            self.runs = get_storage().runs
        return self.runs

    def schedule_store(self) -> Any:
        """The schedule store (opened on first use, closed by :meth:`release_handles`)."""
        if self.schedules is None:
            from cairn.core.storage.schedule_store import default_schedule_store  # noqa: PLC0415 — lazy, as above

            self.schedules = default_schedule_store()
            self._opened.append(self.schedules)
        return self.schedules

    def fingerprint_store(self) -> Any:
        """The fingerprint index (opened on first use, closed by :meth:`release_handles`)."""
        if self.fingerprints is None:
            from cairn.core.storage.fingerprints import build_fingerprint_store  # noqa: PLC0415 — lazy, as above

            self.fingerprints = build_fingerprint_store()
            self._opened.append(self.fingerprints)
        return self.fingerprints

    async def release_handles(self) -> None:
        """Close every handle this context opened itself; never an injected one."""
        while self._opened:
            handle = self._opened.pop()
            closer = getattr(handle, "close", None)
            if not callable(closer):
                continue
            try:
                result = closer()
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 — best-effort cleanup after the operator's own outcome
                self.logger.debug("store handle close failed", exc_info=True)

    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("cairn.operator")
    )

    # Long-running helpers — runtime patches these in for operators that
    # declare ``supports_heartbeat`` / ``supports_cancel``. Default no-ops
    # so operators can call them unconditionally.
    def emit_heartbeat(self, status: dict[str, Any]) -> None:
        """Emit a heartbeat with arbitrary status payload.

        Default no-op. The external-execution runtime (OP6b operator-runner,
        #197) overrides this to call the Temporal heartbeat API.
        """
        return None

    def check_cancelled(self) -> bool:
        """Return True if the run has been cancelled.

        Default returns False. The external-execution runtime overrides
        to consult the Temporal cancel API.
        """
        return False


# ─── Operation (§24.20.12) ────────────────────────────────────────────────


class Operation(Generic[I, O]):
    """Base class for operators.

    Subclass with concrete Pydantic input/output schemas:

    .. code-block:: python

        @register_operator(
            name="clickstack/query_logs",
            version="0.1.0",
            capabilities=["logs.search"],
        )
        class QueryLogsOp(Operation[QueryLogsInput, QueryLogsOutput]):
            input_schema = QueryLogsInput
            output_schema = QueryLogsOutput

            def run(self, inp: QueryLogsInput, ctx: OperationContext) -> QueryLogsOutput:
                ...

    Class-level declarations (§24.20.12):

      - ``input_schema`` / ``output_schema`` — Pydantic classes (REQUIRED)
      - ``config_schema`` — optional Pydantic class of deployment-tunable knobs
        (a subset of ``input_schema``); ADR-0068. Default None (no knobs).
      - ``long_running`` — default False. True → compiler routes to Temporal.
      - ``requires_gpu`` — default False. True → compiler requires GPU executor.
      - ``supports_cancel`` — default False. True → ``cancel()`` is callable.
      - ``supports_heartbeat`` — default False. True → ``heartbeat()`` is callable.
      - ``supports_mock`` — default False. True → ``mock()`` may be called when
        the run is in mock mode.
      - ``side_effect_free`` — default False. True → the operator only READS /
        analyses (no external mutation, no provisioning): a Playground *sandbox*
        run executes it LIVE for real metrics, instead of stubbing it. Full
        ``--mock`` runs still stub every ``supports_mock`` op regardless. Safe
        default: an unmarked op is stubbed in the sandbox (ADR-0073, #836).
      - ``sandbox_safe`` — default False. True → a CHEAP, scoped WRITE (artifact
        put, s3 put, report) that is safe to run LIVE in the sandbox *only* when
        the run's environment is the isolated ``playground`` env (scratch
        backends + TTL), so the report is a real downloadable file. In any other
        env it is stubbed like a normal write. Distinct from ``side_effect_free``
        (which is env-independent). ADR-0073, #844.
      - ``image`` — optional container image; presence routes to external job.
      - ``resources`` — optional :class:`ResourceSpec` declaring minimums.
      - ``capabilities`` — list of capability names this operator provides
        (populated by :func:`register_operator`).

    The runtime sets ``name`` and ``version`` on the class via
    :func:`register_operator`. Subclasses MUST be decorated.
    """

    # Set by @register_operator. Declared as type hints so subclasses
    # without the decorator type-check fail early when registry lookup
    # tries to use them.
    name: str
    version: str
    capabilities: list[str] = []
    #: #2241 B (ADR-0111 D3/P4) — the operator's exact contract identity:
    #: ``contract://<capability-id>/<revision>`` references, one capability ID,
    #: exact revisions. Empty = legacy (not opted in): nothing about the
    #: operator's behaviour changes. Declared through ``register_operator(
    #: implements=[...])`` so the refs are validated once, at registration;
    #: mirrored in ``pack.toml`` and drift-gated like every other entry field.
    implements: tuple[str, ...] = ()

    # Schema declarations — subclasses MUST set these.
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]

    # ADR-0068 (#742) — the OPTIONAL public config surface: a Pydantic model
    # whose fields are a SUBSET of `input_schema` (same names + nested shape)
    # naming the DEPLOYMENT-tunable knobs (thresholds, budgets, limits). A
    # profile supplies values under `[profile.config."<op>"]`; the runtime
    # deep-merges the resolved config UNDER the YAML-bound input (an explicit
    # bound `$input` always wins). `None` (default) = no tunable knobs → the
    # operator exposes no config surface. Distinct from `input_schema` (per-run
    # wired data). Drives `cairn config scaffold`, the console Guardrails form,
    # doctor validation, and audit.
    config_schema: Optional[type[BaseModel]] = None

    # Class-level metadata (overridable in subclasses)
    long_running: bool = False
    requires_gpu: bool = False
    supports_cancel: bool = False
    supports_heartbeat: bool = False
    supports_mock: bool = False
    side_effect_free: bool = False  # ADR-0073 #836 — run LIVE in a sandbox run
    sandbox_safe: bool = False      # ADR-0073 #844 — cheap scoped write; live in isolated playground env
    image: Optional[str] = None
    resources: Optional[ResourceSpec] = None

    # ── governance declarations (#1539 part 3) ────────────────────────
    # The CLASS is the single source of truth for the whole
    # ``[operator."<n>"]`` table: these attrs used to live only in
    # pack.toml, restated by hand with nothing enforcing parity. The
    # table is now GENERATED from the class (`cairn pack sync`) and
    # `cairn doctor` fails on drift (see core/operator/manifest_sync.py).
    # Values validate against the locked enums in core/pack/manifest.py.
    risk: str = "read_only"
    executors: list[str] = ["in_process"]  # noqa: RUF012 — declarative class attr, never mutated
    requires_approval: bool = False
    supports_dry_run: bool = False
    supports_verify: bool = False
    supports_compensate: bool = False
    # Only for operators dispatched via the ``mcp_tool`` executor: the
    # logical MCP server name (profile binds the transport) + tool name.
    mcp_server: Optional[str] = None
    mcp_tool: Optional[str] = None

    # ADR-0103 (#2035) — the reporting contract. REQUIRED, like ``risk``:
    # what this operator reports (stages · series with units · scalars), so
    # the console renders it without knowing the operator (ADR-0102 D7).
    # An operator with nothing to report says ``reports = ReportSpec.NONE``
    # explicitly; a MISSING attribute fails the manifest-sync gate (no
    # backfill exemption remains, #2415). Mirrored as ``[operator."<n>".reports]``.
    # Deliberately declared without a default: the absence is the signal.
    reports: "ReportSpec"

    # ADR-0080 P0 (#1028) — durable-runtime activity budget. The compiler stamps
    # these onto this operator's Temporal node, OVERRIDING the per-kind IO default
    # (retiring the blanket 1800s timeout crutch). Declare what the op needs: a
    # full-corpus PII/dedup scan asks for a large `activity_timeout_seconds`; a
    # cheap probe leaves it None and inherits the modest default.
    # `activity_heartbeat_seconds` sets a heartbeat timeout for fast stuck-
    # detection — set it ONLY on an op that actually emits heartbeats
    # (`supports_heartbeat`), else Temporal fails the activity spuriously.
    activity_timeout_seconds: Optional[float] = None
    activity_heartbeat_seconds: Optional[float] = None

    # ── lifecycle methods ─────────────────────────────────────────────

    def run(self, inp: I, ctx: OperationContext) -> O:
        """REQUIRED — perform the operation.

        Subclasses MUST override. The default raises ``NotImplementedError``
        so that an Operation instantiated by accident (e.g., a typo in a
        registered class) fails loud, not silently.
        """
        raise NotImplementedError(
            f"{type(self).__name__}.run() not implemented — operators must "
            "override run(inp, ctx)."
        )

    def mock(self, inp: I, ctx: OperationContext) -> O:
        """OPTIONAL — return a mock output without real side effects.

        Only called when ``supports_mock = True`` AND the run was started
        with ``--mock``. Default raises ``NotImplementedError``.
        """
        raise NotImplementedError(
            f"{type(self).__name__}.mock() not implemented — set "
            "supports_mock = False or override mock()."
        )

    def cancel(self, ctx: OperationContext) -> None:
        """OPTIONAL — cooperative cancellation hook.

        Only called when ``supports_cancel = True``. The default no-op
        is safe — most operators check ``ctx.check_cancelled()`` from
        inside ``run()`` instead of relying on this hook.
        """
        return None

    def heartbeat(self, ctx: OperationContext) -> dict[str, Any]:
        """OPTIONAL — produce status payload for heartbeats.

        Only called when ``supports_heartbeat = True``. Default returns
        an empty dict.
        """
        return {}


__all__ = [
    "Operation",
    "OperationContext",
    "ResourceSpec",
    "ArtifactStore",
    "SecretStore",
    "MetricsLogger",
    "CostLedger",
    "NullArtifactStore",
    "NullSecretStore",
    "NullMetricsLogger",
    "NullCostLedger",
]
