# ADR-0115 — Pipeline registration, activation and execution identity

- **Status:** PROPOSED — maintainer approved recording this direction and implementation tracking on 2026-09-30. Physical schema/API details below remain proposed pending scoped implementation review. The no-data-migration decision is explicit.
- **Date:** 2026-09-30
- **Owner:** Cairn maintainer
- **Tracking:** #2250
- **Evidence baseline:** local working tree at `3f67d348`, inspected 2026-09-30; 26 focused registry tests passed. No production readiness claim.
- **Preserves:** ADR-0084 compiler/runtime boundaries; ADR-0106 declared-pipeline launch and development-loop exception; existing operator, policy, budget, artifact and run owners.
- **Amends on acceptance:** ADR-0049/0051 registration behavior and populate-on-read; ADR-0101/0084 installation-to-trigger activation coupling. Old workflow-registration compatibility is deliberately removed rather than migrated. No locked ADR is edited.
- **Related:** #2111 pipeline-first surfaces, #2128 run ownership, #1545 package installation, #1762 builder publication, #1159 trigger materialization, #1276 entry-trigger ownership, #2249 qualification.

## Context and verified evidence

The catalog enables template refs (`console/src/views/CatalogTab.tsx:60,95`) through
`POST /registry/enable` (`cairn/core/triggers/routers/registry.py:154`), which calls
`enable_workflow`, not `enable_pipeline`. Empty-default-workspace reads can call
`sync_discovered_workflows` (`registry.py:80`; `workflow_registry.py:720`).
`enable_pipeline` and `sync_discovered_pipelines` exist (`workflow_registry.py:790,853`),
but source search found no production caller for the latter. The helper supplies a
hash without a definition body. `record_provenance` only stores a body when both exist,
and suppresses persistence exceptions (`workflow_registry.py:547`).

Normal launch resolves package files and optionally checks an expected hash; it does not
require an active workspace registration (`dispatch/pipeline_launch.py:174`).
`PipelineBridge.start_pipeline` persists an execution before submitting legs
(`dispatch/pipeline_bridge.py:562`). These are different identities and lifecycles.
Pipeline webhook/watch declarations are explicitly unarmed in
`engine/pipeline_triggers.py:254`; do not describe all triggers as supported.

Read-only local inspection found 22 unprefixed registrations and zero pipeline-prefixed
registrations in `~/.cairn/cairn.db`, alongside 501 pipeline execution records. The old
`~/.cairn/workflows.db` contains both bare and prefixed registry tables. These snapshots
are not proof of the database used by a live deployment. The SQLite rename helper only
renames an old table if the destination is absent (`workflow_registry.py:225`).

**DOCUMENTATION / IMPLEMENTATION CONFLICT:** pipeline-first launch authority coexists
with template-first enablement. Existing helper tests do not prove UI-to-database wiring.

## Decision (proposed)

### D1 — Three primary records, not two and not a standalone workflow entity

| Record / proposed table | Meaning | Created when |
|---|---|---|
| Registered revision / `pipeline_definitions` | Workspace-scoped pipeline version, complete definition snapshot and enablement metadata | Explicit registration succeeds |
| `PipelineRecord` / existing `pipelines` | One admitted execution, pinned to a registered revision and resolved deployment identity | Launch passes admission |
| `Run` / existing `runs` | An executable leg of a pipeline execution | Leg submission |

A flow is a reusable definition component, not an independently enabled deployment.
Its body/schema/hash is captured in the pipeline revision's dependency snapshot. Different
pipelines may capture the same flow. Avoid a new component registry or graph database.
Do not mix a reusable definition with an execution row or rename `pipelines` in this work.
Supporting tables (events, artifacts, schedules, triggers, approvals, budgets, etc.) remain.
`installs` tracks package installation attempts; it is not a legacy workflow table.

The minimal proposed physical shape is one workspace/revision-keyed definition table
containing the canonical pipeline and referenced component bodies/schemas, content identity,
package provenance and lifecycle metadata. Immutable payload and mutable activation metadata
must be separated logically and enforced in writes. No parallel registry service or new
store abstraction: replace the existing registry persistence within its current owner.
Exact columns/indexes and public response schema require scoped review before coding.

### D2 — Explicit registration; reads have no activation side effects

1. Package installation makes assets discoverable; it does not register or enable them.
2. Registration resolves a declared pipeline and recursively validates/captures all referenced
   components through existing parser/compiler/discovery mechanisms. Detect missing refs,
   cycles and unsupported shapes. No new authoring grammar or replacement compiler.
3. Persist the complete revision atomically. Fail the operation if any required write fails.
4. Re-registering identical content is idempotent; changed content produces a new immutable
   revision. Include referenced definition content in identity, not only top-level YAML.
5. Listing endpoints never register definitions, enable execution or arm triggers.

Concurrent registration/enablement must have a deterministic result, one selected revision
per workspace/ref, and no partial success. Implement equivalent SQLite/PostgreSQL behavior.

### D3 — Qualification and activation are explicit outcomes

Validate a registered revision against the selected deployment profile: capability/operator
bindings, implementation/package identities, configuration, required services and policies.
Definition content remains environment-independent. Qualification records identify the
profile/policy/dependency combination; use existing evidence/config owners, not a new store.
Secrets remain scoped references and never enter definition snapshots.

Enablement selects a registered, qualified revision. Trigger activation is explicit and
separately reported, even if one UI action requests both. Unsupported declarations are
blocked/unarmed with reasons, never silently reported ready. Reuse #1159 and the existing
schedule/trigger materializer; no second scheduler. Persist selected revision first, then
reconcile trigger state idempotently; partial failure is visible and retryable. No claim of
a distributed transaction across the registry and external scheduler.

### D4 — One launch door enforces registration

All normal CLI/HTTP/cron/webhook/operator starts go through the existing launch door.
Check workspace membership/enablement and exact registered revision, resolve and verify the
qualified execution dependencies, then execute existing input/policy/service/admission checks.
Fail closed on missing, disabled, stale or incomplete required evidence. Profile resolution
must not install executable dependencies during a run.

Persist `pipelines` only after admission, with registered revision and actual resolved
execution identity. Create `runs` as executable legs submit; gates retain their existing
verdict/review records. Both in-process and Temporal owners obey the same contract.
Registered snapshots are authoritative for definitions; current installed code/resources
must match the recorded selection. Restart/resume must not silently load changed definitions.
Keep ADR-0106's explicit no-record development loop; no new implicit-registration bypass.
Builder publication (#1762) produces a discoverable draft, followed by registration/enable.

### D5 — Disable controls new starts, not historical execution

Disable blocks new admissions and deactivates owned triggers. Existing executions keep their
pinned revision and continue under existing governance; cancellation is a separate action.
A concurrent disable/launch has a defined admission ordering. Trigger delivery still rechecks
enablement at the launch door, including events already queued before disable.
Historical execution and artifact records remain readable after disable or supersession.

### D6 — Remove obsolete tables; no data migration

**Explicit maintainer instruction:** outdated tables should be deleted; no migration of
legacy workflow-registration data is required. Do not implement rename/copy/backfill or
legacy dual-read/dual-write compatibility.

After replacement readers/writers ship, remove these obsolete registry tables from the
explicitly selected Cairn database: `schemas`, `templates`, `installed`,
`workflow_schemas`, `workflow_templates`, `workflow_installed`. Bare names must be verified
as the old Cairn registry schema before dropping; they are not a license to delete arbitrary
application tables with the same names. Apply equivalent cleanup to SQLite/PostgreSQL when
present. This is a schema cleanup/reset operation, not a legacy data conversion.

Stop old writers and pause starts/scheduling during cutover. Inventory/reset only legacy-owned
activation rows that would otherwise keep firing, preserving unrelated schedules/triggers.
Create fresh definition storage and explicitly re-register from installed source packages.
Do not infer legacy enablement or silently reactivate schedules. Old persisted-only definitions
are discarded; package sources must supply any definitions needed again.

Do not delete `pipelines`, `runs`, events, artifacts, installs, secrets or whole database
files. Old execution views must tolerate unavailable retired definition references, label
that limitation, and never fabricate a replacement. Do not auto-resume an in-flight legacy
execution without its required pinned definition; settle/stop it before cutover or block it
explicitly for operator handling. No broad home-directory database sweep. Repeated cleanup
is safe; startup never recreates the removed tables. No database deletion is performed by
this documentation task.

## Boundaries and contracts

- Pack/discovery owns available assets; engine/compiler owns canonical definitions and validation.
- Existing registry/storage owner owns registration persistence; dispatch owns launch checks.
- Existing trigger owner reconciles activation; API/console project these outcomes.
- Contracts: `docs/contracts/pipelines-and-runs.md`, `operators-and-packs.md`, `execution.md`,
  `governance.md`; registry API/OpenAPI and generated console types; additive execution provenance.
- Impact HIGH: registration authorization, persistence semantics, API compatibility and reset.
- No core-to-pack/operator imports, new HTTP dependencies in lower layers, new env knobs,
  new scheduler, new runtime, new entry-point group or duplicate governance checks.

## Delivery and evidence

Storage/revision contract → lifecycle API/UI → launch enforcement → trigger integration
(#1159) → controlled legacy reset → end-to-end qualification. Reset executes only after all
replacement consumers are deployed; cleanup development can proceed earlier on test fixtures.

Required proof includes real SQLite/PostgreSQL readback; concurrent/idempotent registration;
write-failure refusal; immutable old revisions; changed referenced-flow drift; pure GETs;
workspace isolation; disabled/missing registration refusal before records/effects; trigger
partial failure and duplicate delivery; disable/launch ordering; in-process/Temporal parity;
restart with pinned definitions; fresh/reset database operation; old tables absent and never
recreated; unrelated records preserved. Tests replace costly external effects, not the
registration/storage/launch handoff being proved. Update internal contracts/architecture and
public docs in `cairn-landing` with implemented support, not this proposal.

## Alternatives and consequences

- Only `pipelines` and `runs`: rejected; definition existence/enablement would require a fake
  execution or repeated mutable file resolution. It also loses registration before first run.
- Keep the existing six registry tables indefinitely: less immediate schema work but retains
  obsolete model/compatibility paths and conflicts with the explicit cleanup direction.
- New registry service/normalized component ecosystem: unnecessary for this scope. Start with
  complete versioned snapshots in the existing persistence boundary; measure size before adding
  deduplication infrastructure.

Costs: intentionally discarded registration data; fresh explicit registration and activation;
API/client updates; snapshot duplication across revisions. No production scale or recovery
claim until the affected evidence passes. This ADR authorizes tracking, not implementation,
commit, deployment or execution of the database reset.

## Implementation tracking

- #2251 — Pipeline registration: persist complete immutable revisions with strict write outcomes.
- #2252 — Pipeline lifecycle: wire explicit registration and enablement through API and console.
- #2253 — Pipeline admission: enforce enabled registered revisions at the single launch door.
- #2254 — Remove obsolete workflow registry tables without migrating their data.
- #2255 — Qualify pipeline registration-to-execution lifecycle across stores and runtime owners.
- Existing #1159 owns trigger materialization; apply this lifecycle decision there after scoped review, rather than file a duplicate.

## Addendum — Table inventory and configuration consolidation (2026-09-30)

**Status: proposed recommendation, not implemented.** Requested by the maintainer after
reviewing the [database audit](../architecture-fix/database-table-audit.md),
[fixed-table inventory](../architecture-fix/database-table-inventory.csv) and
[observed physical tables](../architecture-fix/database-observed-tables.csv).

### A1 — Apply the inventory recommendations narrowly

The inventory found 58 fixed Cairn-owned table names in source, 29 tables in the configured
PostgreSQL database and 106 distinct names across 36 inspected SQLite files, including
library/vector internals. These snapshots do not establish production usage. Empty tables,
old filenames and missing lazily initialized tables are not deletion evidence.

- D6's six registry tables remain the explicit no-data-migration cleanup scope.
- Keep execution, artifact, approval, event and schedule state within their current owners.
  `pipeline_definitions` remains proposed; `pipelines` and `runs` remain execution records.
- Review `installs` against the chosen package-install lifecycle; it is not the obsolete
  `installed` table and is not included in D6 cleanup.
- Review pilot scoring overlap separately. `pilot_scores` and `pilot_scorecards` are not
  proven duplicates; pilot experiment/run-link state also participates in series tracking.
- Preserve library-owned LangGraph/DBOS checkpoint and workflow tables and vector extension
  shadow tables. Cairn workflow-registration retirement does not authorize their deletion.
- Investigate backend support separately: `inbox`, `playground_upload` and `judge_labels`
  have SQLite persistence but no PostgreSQL table implementation found; `bus_messages`
  and `series_reservations` have PostgreSQL tables without equivalent SQLite persistence.
  These are support differences, not reasons to remove the tables.

### A2 — The three configuration tables are not three independent concepts

| Current table | Stored meaning / selection | Verified consumer | Assessment |
|---|---|---|---|
| `workflow_configs` | One saved operational values map per workspace/template | `dispatch/dispatcher.py:1051` loads it; `:1066` merges a preset over it | Redundant baseline representation; consolidation candidate |
| `workflow_presets` | Named operational values maps with version, creator and optional default | `workflow_config_store.py:52`; dispatcher selected/default preset resolution | Retain operational-preset function; sufficient to represent an optional saved default |
| `input_presets` | Named input values, description and usage metadata; explicit selection | `workflow_config_store.py:91`; `pipeline_presets.py:185` and pipeline launch | Retain separately: different schema, selection and validation semantics |

Both operational models use `values: dict[str, dict[str, Any]]`, currently keyed by
operator. The input model uses `inputs: dict[str, Any]`. All three share the existing
`WorkflowConfigStore` family and SQLite/PostgreSQL implementations; there is already a
shared persistence owner. Adding a generic preset service would not remove an owner.

Operational save validation rejects governance/unknown knobs and coerces supported types
(`workflow_config_store.py:588` onward). Input presets resolve saved values before pack
seeds of the same name, reject missing preset names and undeclared input keys, and let
explicit launch inputs override preset values (`pipeline_presets.py:185–239`).

### A3 — Recommended target: two typed records, one existing owner

**Consolidate `workflow_configs` into the operational-preset mechanism; keep input presets
separate.** A saved operational default becomes one named config preset designated as the
optional default. Do not preserve an additional hidden saved-config baseline underneath it.

Target selection semantics, requiring explicit scoped approval before implementation:

1. A supplied operational preset name selects that preset; missing/invalid selection fails.
2. With no explicit selection, use the designated operational default if one exists.
3. With neither, apply the already-owned profile/declaration defaults; do not invent a saved
   preset or treat a storage failure as absence.
4. The selected preset is the one saved operational layer. Do not merge it over another
   independently saved default preset. Existing permitted explicit run overrides retain
   their own documented precedence; inputs/config/governance are not flattened together.
5. Input presets remain explicitly selected. Retain saved-over-pack name resolution,
   explicit-input overrides and schema validation. Do not add automatic default inputs.

This deliberately changes today's legacy-baseline-plus-preset merge. For example, a saved
baseline `epochs=2` plus a named preset containing only `learning_rate` currently inherits
those epochs. After consolidation the author must put `epochs=2` in the selected preset,
rely intentionally on the declared default, or supply an allowed explicit override. It
must not survive as an undocumented layer. Explain the resolved nonsecret values/sources.

Do not introduce a single untyped `presets` table with a generic payload merely to reduce
table count. Input and operational presets need different validation and default rules.
Keep the existing `workflow_presets` and `input_presets` physical names initially if useful;
renaming those names alone has no architectural value. The target is two responsibilities
and removal of duplicate baseline storage, not a naming-only database rewrite.

### A4 — Keep ownership, identity and reproducibility explicit

Saved presets belong to a workspace and declared pipeline use, not an independently enabled
flow. Reusable flow configuration schemas can still describe leg needs. The exact mapping
from current template/operator keys to pipeline/leg/config addresses must be reviewed across
agentic, data, ML and task paths; do not assume template dispatcher coverage proves parity.
Reuse resolved pipeline/config schemas and the proposed contract ownership in ADR-0111.
Domain definitions must not acquire concrete operator bindings just to persist a preset.

Profiles continue to own deployment wiring, execution choices and adopted policy within
ADR-0112's proposed scope. Presets must not become a second profile or accept governance,
cron, raw credentials, arbitrary backend selection or undeclared knobs. Validate each field
against its actual contract owner rather than classifying it solely by its name.

Save and launch validate against the applicable registered revision/contracts. Record the
selected preset source, version/content identity and effective nonsecret values on execution
provenance. InputPreset currently has no version field, so name/last_used_at alone cannot
identify historical content. Extend existing provenance as needed; do not create a preset
history service. Changed saved presets must not change an admitted execution or its resume.

### A5 — Refusal semantics and scoped retirement

Observed operational resolution is best-effort: config-load errors return `{}`, and a
missing explicitly named preset can select the default (`dispatcher.py:1051–1110`). Required
configuration reads and explicit selections must instead fail visibly before admission;
only optional usage bookkeeping such as `last_used_at` may remain best-effort. Distinguish
"no saved default" from "could not read configuration".

If A3 is accepted, remove `workflow_configs` readers/writers/UI and then drop that table
using the maintainer's no-data-migration policy: no automatic conversion of old baseline
rows into presets. Required values are re-authored explicitly and validated before enablement.
This is an additional proposed retirement scope, not an implicit expansion of #2254's six
approved registry tables. Do not drop `workflow_presets` or `input_presets` under D6.

Required proof: SQLite/PostgreSQL CRUD and default-selection parity; concurrent default
selection leaves at most one; explicit missing name refuses; storage errors do not fall
back; governance/unknown fields refuse; input/config name collisions stay isolated;
preset precedence and the baseline-removal example above; workspace isolation; pipeline
revision drift; all leg kinds; trigger/CLI/API equivalence; pinned values on resume; no
recreation of the retired baseline table. Update API/OpenAPI/console and ADR-0070/0071
precedence documentation through this new decision rather than editing locked records.

**Delivery:** a separately reviewed consolidation slice adjacent to #2252/#2253 after the
registration identity is settled. No new implementation issue or database mutation is made
by this addendum. The alternative is retaining the three tables with documented layering;
it avoids a behavior change but retains duplicate storage and hidden precedence. The
recommended two-record approach removes that duplication without conflating inputs and
operational configuration.
