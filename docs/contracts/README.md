# Contracts — the index

A contract is a shape or a promise that more than one boundary relies on. This index names
every real contract family, who owns and produces it, who consumes it, and where its
authoritative text lives. Tags: **OBSERVED** (verified in code at the cited line, `main`),
**DOCUMENTED** (an ADR or doc says so), **TARGET** (accepted architecture not yet implemented —
never presented as current). Line numbers are absolute (`docs/CLAUDE.md` rule 4);
`tests/core/test_contract_docs.py` checks that every anchor here still resolves.

Rules that apply to every family: the contract is changed only through its owner's boundary;
a public change follows `docs/compatibility.md`; a change that moves ownership or breaks a
frozen contract needs an ADR (`docs/decisions/`), never a silent edit.

| Family | Owner (boundary) | Producer → consumer | Identity / versioning | Stability | Source of truth (code) | Doc | ADRs |
|---|---|---|---|---|---|---|---|
| **Data** — dataset versions, roles, refs, verdicts | Storage (registry) + Governance (verdict) | readiness/intake legs, imports → launch doors, training legs | `dataset:<name>@N`, version rows over content-addressed artifacts | public, additive | `src/cairn/core/storage/artifacts.py:94-135`, `:395-398`, `:1238` | [data.md](data.md) | ADR-0080, ADR-0102 D1 |
| **Artifact** — `ArtifactRecord`, descriptors, lineage | Storage | engine descriptors → control-plane registration → every consumer | `id` = content hash; immutable rows; `parents` | public, additive | `src/cairn/core/storage/artifacts.py:55-70`, `src/cairn/core/dataflow/contract.py:226-250` | [artifacts.md](artifacts.md) | ADR-0080, ADR-0081 D3 |
| **Pipeline / run** — `PipelineDef`, `PipelineRecord`, `StepState`, `Run`, `RunEvent` | Engine (defs, plan) · Dispatch (bridge) · Storage (records) | pack YAML → plan → record → per-leg runs → events | `pipeline_id`, `run_id`, `template_ref`, `seq` | public (locked surfaces) | `src/cairn/core/engine/pipeline.py:62`, `:152`, `src/cairn/core/storage/pipeline_store.py:50`, `:80`, `src/cairn/core/storage/protocols.py:136`, `:324` | [pipelines-and-runs.md](pipelines-and-runs.md) | ADR-0084, ADR-0086, ADR-0101, ADR-0105, ADR-0106 |
| **Operator / pack** — `Operation`, `pack.toml`, entry-point groups | Operators (substrate) · Packs | pack authors → registry/loader → dispatch | `pack/op` names, manifest version, entry-point group names | public (locked: `Operation`, `pack.toml`) | `src/cairn/core/operator/protocol.py:261`, `src/cairn/core/pack/manifest.py:309`, `src/cairn/core/pack/loader.py:591` | [operators-and-packs.md](operators-and-packs.md) | ADR-0020, ADR-0045, ADR-0055, ADR-0103 |
| **Capability** — name/consumes/produces/preconditions/… | Training architecture (TARGET) | planner/validator (TARGET) | namespaced names (`training.sft`) | descriptors shipped; composition validator TARGET | capability descriptors ship as data in `contracts/*/definitions/capabilities/*.json` (36 across 16 of the 17 `cairn-contracts-*` distributions; `train.finetune` and `train.eval_loss` in `cairn-contracts-train`, ADR-0127 D4), checked by `src/cairn/core/pack/contracts.py:290`; the composition validator is #2193 (open) | `docs/architecture/training/15-contracts.md`, `16-…` | FREEZE-v1 |
| **Execution / dataflow** — `DataflowSpec`, `DataflowEngine`, engine selection, handles | Runtime/dataflow contract (core) · engines (operator pack) | bridge/leg executor → engine → result | `spec_key`, `EngineJobHandle {job_id, engine, meta}` | public (locked: `DataflowSpec`) | `src/cairn/core/dataflow/contract.py:179`, `src/cairn/core/dataflow/engine.py:76`, `src/cairn/core/dataflow/engine_registry.py:83` | [execution.md](execution.md) | ADR-0081, ADR-0082, ADR-0089, ADR-0104 |
| **Governance** — policy decisions, gates, doors, verdicts, budget, audit | Governance | policy/pack data → launch door / run admission → records + audit | decisions on the run/record; audit events | public behaviour | `src/cairn/core/policy/pipeline_gates.py:93`, `src/cairn/core/approval/enforce.py:116`, `src/cairn/core/dispatch/dispatcher.py:837-879`, `src/cairn/core/admission_door.py:114`, `src/cairn/core/obs/audit.py:140` | [governance.md](governance.md) | ADR-0034, ADR-0053, ADR-0102 |
| **Storage** — persisted schemas, migrations | Storage | stores → every reader | sqlite + postgres twins; `add_missing_columns` migrations | internal, additive columns | `src/cairn/core/storage/migrations.py`, `src/cairn/core/storage/{sqlite,postgres}/` | `docs/architecture/09-storage-and-provenance.md` | ADR-0050, ADR-0058 |
| **API / CLI** — HTTP routes, console contract, CLI verbs | Surfaces | server → console/client/MCP | `console/openapi.json` (oasdiff-gated) | public; breaking needs `api-break` + console change | `console/openapi.json`, `scripts/dump_openapi.py`, `src/cairn/cli/` | `CONTRIBUTING.md` §The API contract | ADR-0085 |
| **Event** — `RunEvent`, bus topics, SSE | Storage (events) · core bus | dispatcher/bridge → bridge, SSE, console | `run_id` + monotonic `seq`; topics `run.<type>`, `pipeline.<event>` | public shape, additive types | `src/cairn/core/storage/protocols.py:324`, `src/cairn/core/dispatch/pipeline_bridge.py:78`, `:2330` | [pipelines-and-runs.md](pipelines-and-runs.md) §Events | ADR-0010, ADR-0044 |
| **Integration** — MLflow, Metaflow, SkyPilot, vLLM, storage/compute providers | the adapter's operator pack behind a core boundary | engine/operator adapters ↔ external systems | adapter-owned | engine protocol + operator schemas are the stable side | see doc | [integrations.md](integrations.md) | ADR-0063, ADR-0089, ADR-0098, ADR-0104 |

Not documented here on purpose: **secrets/connections** (a deployment concern — `docs/security.md`,
`docs/connections.md`), **LLM provider settings** (`services/llm`, a service not a boundary
contract), and the **console UI contract** beyond OpenAPI (`console/DESIGN.md`).
