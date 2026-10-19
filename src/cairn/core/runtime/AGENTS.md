# cairn/core/runtime — how a compiled agentic graph executes

**Purpose.** The `Runtime` protocol (`base.py`) and its implementations: `InProcessRuntime`
(default), `DBOSRuntime` (`CAIRN_RUNTIME=dbos`), `TemporalRuntime` + `TemplateWorkflow` +
`cairn-worker` (`CAIRN_RUNTIME=temporal`); the LangGraph checkpointer tiers
(`checkpointer.py`, `CAIRN_CHECKPOINTER`); the zombie-run reconciler; and the **durable
pipeline owner** (`temporal/pipeline_workflow.py` + `pipeline_activities.py`) — the Temporal
twin of `dispatch/pipeline_bridge.py`.
**Does not own.** Data/ml flow execution (that is `cairn/core/dataflow/` + engine packs —
a different waist: `DataflowEngine`), graph construction (compiler), leg semantics
(shared through `dispatch/leg_hooks.py`).

**Contracts.** `Runtime.invoke(compiled_graph, state, *, run_id, template_name,
template_sha)`; Temporal workflow bodies are pure orchestration — every cairn side effect
lives in an activity; `retries=0` cairn-side for ml legs (ADR-0082 D2); the run store is the
pipeline checkpoint (ADR-0084 D6).

**Allowed deps.** `storage`, `dispatch` (activities build a dispatcher — a known reverse
edge), `engine` (types), `bus`, `approval`, `operator`, `pack`, `dataflow`, `obs`,
`gate_notify` + `notifier` (the approval-pause notification a Temporal flow leg sends, as
the bridge does — #2299, maintainer 2026-10-07), `liveness` (the probe timeout constant only;
the probe itself is injected by the server, ADR-0125 D3).
**Forbidden.** operator packs, `cairn.packs.*`, `triggers`, `cli`; **never** import
`temporalio`/`dbos` at module top outside the `temporal/`/`dbos/` subpackages (optional
extras; `test_temporal_subpackage_imports_without_sdk`).

**Testing.** `tests/core/test_runtime.py`, `test_temporal_*.py`, `test_dbos_runtime.py`
(`-m dbos`), `test_checkpointer_pool.py`, `test_run_reconciler.py`, `test_reconciler_*.py`,
`test_durable_restart.py`, `test_temporal_owner_leg_kinds.py`, `test_temporal_owner_readopt.py`,
`scripts/temporal_smoke.py` (live).

**Common mistakes.** Implementing a leg behaviour in `PipelineBridge` but not in
`pipeline_activities.py` (#1429); non-deterministic code in a workflow body; a checkpointer
pool per call instead of per loop; treating `CAIRN_RUNTIME` as the flow-engine selector (it
is not — see `dataflow/engine_registry.py`).

**Read.** `docs/architecture/07-runtime.md`, ADR-0024, ADR-0035, ADR-0036, ADR-0069 Phase 4,
ADR-0084 D1/D6, ADR-0086.
