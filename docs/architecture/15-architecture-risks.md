# Architecture risks and findings

Ranked. Every finding is OBSERVED at HEAD `81d876ae` unless tagged DOCUMENTED (from an
existing audit, not re-verified here). **Nothing here was fixed by this documentation
pass; these are inputs to later decisions.**

## CRITICAL
| # | Finding | Evidence | Why it matters |
|---|---|---|---|
| C1 | `src/cairn/cli/commands/run.py:5-12,253,341,405`; contrast `src/cairn/core/dispatch/dispatcher.py:737,835-879` | ADR-0106 D6 made the in-process path the explicit **dev loop** (`--interactive | --mock | --dry-run`: no pipeline, no record); plain `cairn run <ref>` goes through the governed start, which refuses a bare flow ref (D1). Residual risk: the dev loop fires only the per-operation gate — doors and budget reservation do not apply, by design. Downgraded from a conflict to a documented shape. | The product thesis is "one door"; ADR-0105 D1 names `cairn run` as going through the one-leg pipeline. The per-operation gate still fires, so this is a governance *coverage* gap, not a bypass of approvals. |
| C2 | **Authorization within the governance plane** (DOCUMENTED, `docs/audits/architecture-audit-2026-08-21.md` red flag 1, issue #1486): any authenticated user can approve any gate; governance-config writes not behind `require_admin`. | audit §1; not re-verified in this pass | Insider escalation inside the control plane. Verify current state before acting. |

## HIGH
| # | Finding | Evidence |
|---|---|---|
| H1 | **God-modules at the chokepoints.** `PipelineBridge` 2,145 lines / 70 methods; `RunDispatcher` 1,169 lines; `_compile_bt` 703-line function; `build_app` 591 lines; router builders of 300–660 lines with route handlers as closures (`routers/runs.py:92`, `admin.py:47`, `pipelines.py:24`, `insights.py:48`, …). | size scan; `pipeline_bridge.py:270`, `dispatcher.py:223`, `compiler.py:1135`, `server.py:505` |
| H2 | **`src/cairn/core/*.py` is an un-bounded shared kernel**: 60 modules (~20k lines) with the highest fan-in and fan-out, mixing doors, stores, seeds, registries, deploy checks and product features (pilot, judge agreement, recommender). No `CLAUDE.md`, no ownership statement, no import rule. | `01-system-map.md`, `02-dependency-model.md` |
| H3 | **Storage sprawl and two config planes.** ~30 hand-rolled memory/sqlite/postgres triads (102 store-like classes); 280 env reads; 153 `CAIRN_*` names vs 35 in the settings inventory; legacy per-store knobs. Postgres coverage incomplete → prod HA gaps (P13). | counts in `09-storage-and-provenance.md`; `docs/audits/rearchitecture-problem-inventory.md` P9–P14 |
| H4 | **Lower layers import the HTTP layer.** Reduced by #2416: the trigger grammar, spec and presets moved to `engine/triggers_*.py`, the schedule/trigger stores and inbox to `storage/`, the token helpers to `core/auth_tokens.py`. Remaining: `app_client.py` (→ `triggers.{main,server}`), `pack/composition.py` (→ the `TriggerProvider` registry), `showcase_seed.py` (#2418). | edge detail in `02-dependency-model.md` |
| H5 | **Core imports a domain pack** (`cairn.packs.training`) to locate data directories — violates the documented hard rule; no test guards `cairn.packs` imports from core (only the MCP server is tested). | `models_catalog.py:100`, `recommender.py:102` |
| H6 | **Test discovery gaps in operator packs.** 14 packs with `tests/` not in `testpaths`; 6 packs with no tests; legacy `tests/operators/` tree still present; DESIGN.md exists for 9/36 packs (BACKFILL). | `pyproject.toml testpaths`; `ls cairn/operators/*/*/tests` |
| H7 | RESOLVED (#2413): the undiscoverable packs `operators/builtins/rag_parse` and the domain packs `redteam`, `redteam_kb` were retired (deleted, and dropped from `cairn.lock`). `operators/official/redteam` was wired in by #2289, though its own `tests/` are still outside `testpaths`. | |
| H8 | **Implicit global state**: ~30 process-global singletons with `reset_*` helpers (redactor, audit sink, policy engine, storage, bus, checkpointer, secret provider, job store, feature store, catalog cache, `_DISPATCHER` in Temporal activities). Tests depend on manual resets; production correctness depends on construction order. | `grep reset_` list in recon |
| H9 | **Cycles held open by lazy imports** annotated `# cycle`: engine↔dispatch, approval↔operator↔engine, dispatch↔runtime, policy→approval→engine.runner→dispatch, storage↔services. Any new top-level import in these modules can create an import-time failure. | `02-dependency-model.md` |
| H10 | **Budget reservation does not cover `task:` legs.** | only `dispatcher.py:765`, `leg_executor.py:281` call `reserve(` |
| H11 | **Read path scales with history** (DOCUMENTED, 08-21 audit red flag 2): runs-list hydrates full JSONB blobs; no retention on `run_events`/`bus_messages`. | not re-verified |

## MEDIUM
| # | Finding | Evidence |
|---|---|---|
| M1 | **Vocabulary debt**: template / flow / workflow / pipeline / bundle all live in identifiers (`template_ref`, `workflow_registry`, `bundle_pipeline_id`, `bundle_ref`, `run_bundle` = an unrelated zip). "Gate" has three meanings (P6). | glossary `03-core-concepts.md` |
| M2 | **Two training paths** (ml flow vs agentic template) with overlapping governance semantics; the pack description documents only one. | `10-training.md` |
| M3 | **Operator packs reaching past the operator contract**: `monitor_threshold` → dispatcher/bridge/runner; `ingest` → `storage.schedule_store`; `notify_slack` → `engine.schema`; `dataflow` → `operator_remote` (pack→pack); several → `cairn.services`/`cairn.integrations`. | `02-dependency-model.md` |
| M4 | **Services tier runs pack-provided operators**: `services/rag/inject.py` (`rag/search`) resolves it by ref through the operator registry (no pack import since #2415; `rag/ingest.py`'s `rag/parse` lookup was dropped with the retired operator, #2451); the C-6 layering test covers every `src/cairn/services` module (#2425). | |
| M5 | RESOLVED (#2409): the stale narrative `docs/architecture.md` and the root `architecture.md` pointer were deleted; `CONTRIBUTING.md` reading order now starts at `00-overview.md`. | |
| M6 | **Console read-API keeps one flagged route**: with no gateway configured (or answering) `/console/api/gateway` returns an EMPTY `GatewaySummary` flagged `X-Cairn-Stub` (no sample figures since the gateway stub was removed); `console/stubs.py` itself was deleted (#2368). | `console/api.py` `get_gateway` |
| M7 | **Temporal owner parity is by convention**: `PipelineBridge` and `pipeline_activities.py` must implement the same leg semantics; shared via `leg_hooks.py` after #1429, but new bridge features can still land on one side only (has happened: #1429). | `dispatch/leg_hooks.py` docstring |
| M8 | **Deprecated surfaces kept alive**: `MetricsLogger`/`ctx.metrics` (ADR-0103), `templates/` alias dir, `dataflows/` alias dir, legacy `python_function_registry` (778 lines of domain functions in core, P17). | |
| M9 | **Engine-neutrality of `DataflowSpec` is reviewed by hand** — no test asserts the absence of engine-specific fields. | `dataflow/contract.py` |
| M10 | **Console↔engine contract is broad**: 214 paths, 28 under `/ui/api/*` (the `/ui` pages went in #2426) and 23 under `/console/*` alongside the resource routes; `oasdiff` gates breaking changes but the surface itself has no owner map. | `console/openapi.json` |
| M11 | Domain pack `incident/__init__.py` imports Python (`cairn_platform.integrations.observability.mock.backend`) — the one pack that is not purely `Pack.from_manifest`. | `packs/incident/__init__.py:16` |
| M12 | `guard_context` (capability guard for ops) is reachable only from SDK-generated code (DOCUMENTED 08-13; still true). | `packages/cairn-sdk/src/cairn_sdk/compiler.py:474` |

## LOW
| # | Finding |
|---|---|
| L1 | `src/cairn/core/engine/__init__.py` is empty (no public surface declared); `src/cairn/core/pack/__init__.py` and `operator/__init__.py` re-export without `__all__` audits (ruff F401 deliberately off). |
| L2 | `compiler.py` docstring says "three flow types" and lists two. |
| L3 | `src/cairn/core/light.py` is untracked in the working tree (not part of the committed architecture; excluded here). |
| L4 | README "Eight packs / 14 pipelines" counts are stale (9 entry-pointed packs; training alone ships 13 pipeline files). |
| L5 | ADR-0020 (locked) still specifies `type = "domain"` and "zero .py, doctor-enforced"; shipped manifests use `kind` (ADR-0083 D4) and every domain pack has an `__init__.py`. The lock was amended in practice without a superseding note in `docs/decisions/README.md`. |

## DOCUMENTATION / IMPLEMENTATION CONFLICTS
| Doc says | Code shows | Where |
|---|---|---|
| ADR-0084 D6: "No Temporal at the pipeline tier" | `PipelineBridge.start_pipeline` hands the pipeline to `PipelineWorkflow` when `CAIRN_RUNTIME=temporal` | `pipeline_bridge.py:558-582` |
| ADR-0084 D5: the one cross-pack ref (`training/finetune` → `dataset-readiness/check`) is internalised | `flows/finetune.md` still subgraphs `dataset-readiness/check` (compiler warns) | `cairn/packs/training/flows/finetune.md` |
| ADR-0106 D1/D6 (supersedes ADR-0105 D1): a bare flow ref is refused at the door; the dev loop runs in-process by decision | RESOLVED BY DECISION 2026-09-09: `resolve_start_ref` raises `FlowNotRunnableError`; `cairn run --interactive|--mock|--dry-run` is the documented dev loop and writes no record | `dispatch/start.py:69,179`, `cli/commands/run.py:5-12` |
| ADR-0020: domain pack = zero `.py`, doctor-enforced; `type = "domain"` | one `__init__.py` per pack; no doctor check found; manifests use `kind` | `cairn/packs/*/__init__.py` |
| README: "Eight packs ship … 14 runnable pipelines" | 9 entry-pointed domain packs (+2 undeclared); pipeline count differs | `pyproject.toml`, `cairn/packs/*/pipelines` |
| `pyproject.toml`: ask/sdk boundary "CI-enforced" | a boundary test exists only for `client/` | `tests/core/test_cairn_client_contract.py` |
| `cairn/operators/CLAUDE.md` §A (formerly root rule 5): operator tests live in the pack, never `tests/operators/` | `tests/operators/` still holds 13 pack dirs (#134 migration open) | |

## Missing enforcement (should become automated tests)
1. AST test: no module under `src/cairn/core` imports `cairn.packs.*` (mirror of `test_core_never_imports_an_operator_pack`).
2. AST test: `src/cairn/services` imports no `operator_*` package.
3. AST test: `ask/` and `sdk/` import nothing from `cairn`.
4. Layering test: `engine/`, `storage/`, `pack/`, `operator/`, `dataflow/` import nothing from `cairn.core.triggers` (start with top-level imports; then lazy).
5. Contract test: `DataflowSpec` field set is frozen to an allow-list.
6. Walk test: every `kind = "domain"` pack ships ≥1 `pipelines/*.yaml` and only `__init__.py` as Python.
7. Discovery test: every `cairn/operators/*/*/tests` dir appears in `testpaths` (or move to a single conftest, #134).
8. Workspace test: every on-disk operator pack is a `[tool.uv.workspace]` member.
9. Governance parity test: every leg kind that can spend reserves budget (task legs).
10. CLI parity test: `cairn run <ref>` reaches `resolve_start_ref`/`launch_*` (or a documented exemption).
11. Size budget test (ratchet): `PipelineBridge`, `RunDispatcher`, `_compile_bt`, `build_app` may only shrink.
