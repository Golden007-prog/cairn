# Architecture health report — 2026-09-10 (HEAD `81d876ae`)

This is an assessment, not a plan. Every item links to evidence in the numbered documents.
Recommendations are **not implemented**.

## Strengths (verified)
1. **The governance thesis is real in code.** `enforce_gate` is one function called from the compiler node, the task-leg path and the flow-leg path (as `enforce_flow_gate`); redaction lives in the one chat-model constructor and is tested against real LangChain machinery; policy-injected gates fire at the single pipeline launch door before a record exists ([08](08-governance.md)).
2. **Extension by entry points, not imports.** Packs (`cairn.packs`), operators (`cairn.operators`), engines (`cairn.dataflow_engines`), stages (`cairn.flow_stages`) — and an AST test keeps core from importing any operator pack ([02](02-dependency-model.md)).
3. **Leg-kind routing is a single door.** `_submit_step` routes by kind; a data/ml leg reaches an engine adapter without LangGraph (tested) ([13](13-pipelines.md)).
4. **Identity at admission.** `Run.template_ref`, `bundle_pipeline_id`, pins and content hashes give a coherent provenance chain ([09](09-storage-and-provenance.md)).
5. **Contract-gated console.** `console/openapi.json` is committed, freshness-checked and breaking-change-diffed in CI.
6. **Manifest ↔ code drift is a failing test**, and operator packs must carry a design record ([05](05-operators.md)).
7. **Test discipline**: 5,046 + 1,520 test functions, behavioural, hermetic conftest.
8. **Decisions are written down**: 106 ADRs, honest audits in `docs/audits/`, probe-backed `status.yaml`.

## Weaknesses
1. Governance coverage has one uneven edge: the CLI's in-process template run and unreserved `task:` legs ([15](15-architecture-risks.md) C1, H10).
2. Complexity is concentrated exactly at the chokepoints (bridge, dispatcher, compiler, app factory) — the places a reviewer most needs to read.
3. `src/cairn/core/*.py` has become an unowned shared kernel.
4. Storage and configuration have no single substrate (documented since July; ADR-0083 R3/R4 unbuilt).
5. Layering is inverted in places (engine → HTTP-layer grammar; core → domain pack) and cycles are held open by lazy imports.
6. Vocabulary carries its history (template/flow/workflow/pipeline/bundle; three "gates").
7. The narrative architecture doc (`docs/architecture.md`) had drifted from the code — deleted in #2409.

## Complexity hotspots (ranked)
| Rank | Hotspot | Measure |
|---|---|---|
| 1 | `src/cairn/core/dispatch/pipeline_bridge.py PipelineBridge` | 2,145 lines, 70 methods |
| 2 | `src/cairn/core/dispatch/dispatcher.py RunDispatcher` (+ `run_template` 283 lines, `_run_template_task` 205) | 1,391 lines |
| 3 | `src/cairn/core/engine/compiler.py _compile_bt` | 703-line function in a 1,947-line module |
| 4 | `src/cairn/core/triggers/server.py build_app` | 591 lines |
| 5 | `src/cairn/core/triggers/routers/runs.py build_runs_router` and siblings (`admin` 512, `pipelines` 482, `insights` 437, `builder` 367, `connections` 354, `run_control` 350, `capture` 344) | route handlers as closures |
| 6 | `src/cairn/core/*.py` top level | 60 modules, ~20k lines, no boundary |
| 7 | `src/cairn/core/pack/manifest.py` | 1,584 lines (`load_manifest` 204, `_validate_operators` 197) |
| 8 | `src/cairn/core/storage/artifacts.py` | 1,286 lines, two 320-line store classes |
| 9 | `src/cairn/core/dispatch/leg_executor.py execute_leg` | 300-line function |
| 10 | `src/cairn/core/operator/runtime.py` | 1,100 lines; `invoke_operator_job` + durable remote path |

## Boundary violations (actual)
- Core → domain pack: `models_catalog.py:100`, `recommender.py:102` → `cairn.packs.training`.
- Lower layers → `triggers` (lazy, 6 sites after #2416): `pack/composition.py` (the `TriggerProvider` registry), `app_client.py` (2), `showcase_seed.py` (3, #2418).
- Services → operator pack: `services/rag/inject.py:64`.
- Operator pack → engine internals: `monitor_threshold` (dispatcher, bridge, launch, runner), `notify_slack` (engine schema).
- Pack → pack: `operator_dataflow` → `operator_remote`.

## Documentation gaps
- No document owned "what may live at `src/cairn/core/*.py`" — now partially covered by `01-system-map.md`, still no rule.
- The leg-kind × governance matrix existed only in code comments (now [08](08-governance.md)).
- No map from console view → route → store (only the OpenAPI file).
- ADR-0020's amendments (`kind`, `__init__.py`) are undocumented in the ADR log.
- The two training paths are not described side by side anywhere in `docs/` (now [10](10-training.md)).

## Test gaps (important)
- Core→packs import rule; services→operator rule; ask/sdk boundary; engine→triggers layering (see [15](15-architecture-risks.md) "Missing enforcement").
- 15 operator packs' tests not discovered; 6 packs untested.
- `DataflowSpec` engine-neutrality; golden-dataset guard; artifact tag mutability.
- CLI ↔ dispatcher parity of admission doors.
- Task-leg budget reservation.

## Recommended next steps (not implemented)
**DO NOW** (cheap, no behaviour change, high leverage)
1. Add the AST/layering tests listed in [15](15-architecture-risks.md) "Missing enforcement" items 1–4, 7, 8 — they only *observe*; where they fail today, register the current offenders in an explicit allow-list that may only shrink (the `BACKFILL` pattern already used for DESIGN.md).
2. Decide C1: either route `cairn run` through `resolve_start_ref`/`launch_*` (ADR-0105 D1) or write down the local-dev exemption in an ADR.
3. ~~Refresh or retire `docs/architecture.md`~~ — done: retired in #2409.
4. Add `cairn/operators/*/*/tests` to `testpaths` (or the single-conftest hoist, #134). (`rag_parse`/`redteam`/`redteam_kb`: retired in #2413.)

**DO LATER** (needs an ADR and a plan)
5. ~~Home `triggers/declared.py`'s grammar next to `engine/schema.py`; move trigger stores out of the HTTP package~~ — done in #2416 (ADR-0120 D6).
6. Give `src/cairn/core/*.py` a shape (e.g. `doors/`, `stores/`, `product/`) — a move, not a rewrite; count readers first.
7. One storage substrate + one config plane (ADR-0083 R3/R4) — largest payoff, largest blast radius.
8. Split `PipelineBridge` by concern (advance/route, leg submit, approvals, Temporal hand-off) behind the same public methods; ratchet sizes.
9. Retire dual vocabulary in identifiers only when a migration can carry stored records.

**DO NOT TOUCH YET**
- `enforce_gate` / `resolve_gate` / `build_chat_model` / `inject_policy_gates` — the chokepoints are correct and tested; any refactor must re-prove reachability.
- `_compile_bt` — ADR-0084 D8: amend, never replace; a rewrite re-opens gate enforcement, durability and replay.
- The `PipelinePlan` IR and `PipelineRecord` shape — stored records depend on them.
- `client/` surface — it is the console/CLI/ask boundary; changes ride the OpenAPI process.
