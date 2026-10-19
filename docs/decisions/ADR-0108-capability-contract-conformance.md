# ADR-0108 — Capability contract conformance through existing pack and pipeline boundaries

- **Status:** PROPOSED — maintainer decision pending. The request to file this ADR authorizes documentation and tracking, not adoption or implementation.
- **Date:** 2026-09-20
- **Decision/follow-up epic:** #2230 — blocked pending maintainer decision; existing implementation ownership is preserved below.
- **Evidence:** local HEAD `3a69f32fa7fee82cc75662776bbe6627b16d90a8`, including the reviewed working tree; not a claim about newer remote code.
- **Related work:** #2186 (training delivery), #2193 (Phase 1 composition validation), #2200/#2201 (preference data and DPO), #2191 (checkpoint semantics), #2211 (later selection).
- **Authority:** proposes an additive extension to ADR-0020's locked pack metadata surface. Preserves ADR-0026, ADR-0084, ADR-0103, ADR-0104 and ADR-0106. Supersedes no accepted decision while proposed. The frozen training baseline remains authoritative.

## Context

The reviewed 42-section proposal correctly identifies distributed semantic assumptions,
but proposes four new manifest kinds and an executor extraction where existing carriers
already cover much of the responsibility. The narrow unresolved choice is how to
make semantic compatibility enforceable without duplicating them.

| Evidence | Finding |
|---|---|
| DOCUMENTED: `docs/architecture/training/FREEZE-v1.md:7`, `:11` | Capabilities already have a frozen target contract; artifact identity and composition are existing concepts. |
| DOCUMENTED: `docs/architecture/training/16-training-planning-and-composition.md:170` | Existing manifests and flow metadata are the proposed carriers; validate known compositions before selecting graphs. |
| OBSERVED: `cairn/core/engine/pipeline.py:62`, `:687` | Typed wires and structural checks already exist; they do not establish complete semantic compatibility. |
| OBSERVED: `cairn/core/storage/artifacts.py:53`, `cairn/core/dispatch/leg_executor.py:436` | Artifact identity, schema, parents and candidate registration already exist; URI-only interfaces remain a coverage problem. |
| DOCUMENTED: `docs/decisions/ADR-0103-operator-reporting-contract.md:16` | Reporting already has an operator contract. |
| OBSERVED: `cairn/core/operator/config.py:114`, `cairn/core/operator/executor.py:58` | Configuration precedence and operator executors already exist. |

## Proposed decision

**D1 — One authoritative declaration per capability contract.** Reuse installed pack
discovery, operator manifest declarations and flow metadata. Capabilities remain
namespaced pack-owned names, not a core enum. Generic core validation understands
contract structure; domain packs own domain meaning. Do not add a Capability,
DataContract, ReportContract or Executor registry, entry-point group or store.

**D2 — Extend compatibility checks, not the execution model.** Reuse pipeline structural
and typed-wire validation, adding semantic checks for consumed/produced artifact roles,
preconditions, compatibility and engine-neutral resource requirements. Direct and
generated declared pipelines must converge on the same validator before side effects,
policy/admission doors and run creation. A semantic check supplies evidence to existing
governance; it does not replace or duplicate a policy evaluator.

**D3 — Identity and meaning are distinct.** Reuse immutable artifact identities,
descriptors, schema and lineage. A URI locates bytes; a role describes intended use;
a format describes representation. A capability references these without creating a
second identity system. Dataset facts remain distinct from request-specific admission.
Validate model/base/tokenizer and dataset-role compatibility where declared. SFT/DPO
are optimization algorithms; LoRA/QLoRA are parameterizations. Candidate outputs can
be adapters or full models, according to the implementation's declared support.

**D4 — Make compatibility explicit and reproducible.** Capability contract revision,
operator version and artifact digest are separate identities. Execution evidence must
record the resolved contract revision and implementation/configuration used through
existing plan/run/artifact carriers. A retry must not silently re-resolve to a different
implementation or incompatible checkpoint consumer. Unknown or missing metadata for
an opted-in required check produces an explicit refusal, never a success verdict.
Legacy definitions retain their existing structural checks and are clearly identified
as not semantically verified; expanding required validation to them needs a reviewed
migration. `cairn.lock` remains an integrity closure, not a semantic version resolver.

**D5 — Preserve execution and governance boundaries.** Declared pipelines still enter
through the one launch door. Agentic Runtime, DataflowEngine and operator executor are
different boundaries. Keep engine selection in deployment profile/environment; reject
engine names in semantic capability metadata. Preserve per-leg runs, operator gates,
aggregate flow gates, budget/redaction/audit, and bridge/Temporal parity. The documented
local development exception remains governed by ADR-0106 D6.

**D6 — Bound adjacent work.** This ADR does not change `TrainSpec`, configuration
precedence or reporting grammar. Configuration ownership is a separate decision if a
public shape changes; semantic categories must coexist with operational/governance
scope, with flat-input compatibility initially. LoRA rank/alpha have portable semantic
meaning and must not automatically be classified as vendor-only knobs. First reuse
`ReportSpec` for shared metrics; specify units, aggregation, dataset/model identity and
applicability. Evaluation loss is conditional and comparable only with matching
measurement context. No blanket reproducibility guarantee follows from a seed.

## Approval prerequisites and exact grammar

This is a direction decision, not a complete replacement for the locked manifest schema.
Before changing production contracts, #2193 must present a concrete additive schema
delta for owner review: declaration ownership and duplicate-provider rules; reference
and revision syntax; supported-version matching; flow/manifest consistency; required
versus legacy metadata; unknown-field refusal; and record hydration compatibility.
Use existing JSON Schema/Pydantic mechanisms. Do not implement the original proposal's
illustrative YAML as if it were an accepted grammar. Any additional locked-surface
change needs a new ADR rather than an edit to an accepted ADR.

## Alternatives

1. **Keep distributed conventions.** Least migration work, but leaves the known semantic
   refusal gaps and frozen target unmet.
2. **Extend existing carriers (recommended).** Adds metadata and conformance obligations;
   keeps one discovery, validation and execution model.
3. **Adopt four registries and a universal executor.** Reject as the initial migration:
   duplicates authority, increases versioning/load-order costs and conflicts with the
   existing execution boundaries without a demonstrated blocker.

## Delivery and acceptance evidence

Existing delivery ownership stays with #2186/#2193; this ADR does not create competing
implementation issues. #2193 owns SFT/reference composition proof, #2201 adds DPO, and
#2211's graph selection remains later. The frozen text describes SFT and DPO as Phase 1
proofs while #2193 sequences DPO separately; final baseline proof still requires both.

- Accept compatible reference compositions; reject incompatible artifact/model/data
  handoffs, absent required identity, and retry without acceptable checkpoint evidence.
- Reject DPO without preference data in #2201; prove valid DPO as well as refusal.
- Prove identical direct/generated launch outcomes, before side effects, including
  bridge/Temporal paths where affected; preserve policy denial and recorded identity.
- Prove manifest/class agreement, dependency boundaries and deterministic discovery;
  no core imports of domain/operator packs and no operator-pack-to-pack imports.
- Prove legacy input/manifest behavior and explicit unverified status, new required
  metadata refusal, version mismatch and retry pinning. No bypass through `extra`.
- If persistence changes, prove SQLite/Postgres migration and old-row hydration; if
  APIs change, align OpenAPI/client/console. These are conditional, not added scope.
- Select workload sizes, metadata cardinalities and measurable latency/memory/recovery
  limits with the maintainer before making scale claims. Measure control-plane metadata
  validation; dataset rows stay in the data plane. No production-scale claim yet.

Historical review evidence: 65 targeted tests passed for typed composition, executor
selection, reporting and training configuration. Those tests prove current mechanisms,
not this proposal. New semantic behavior requires its own positive and rejecting cases.

## Consequences

Contracts become more inspectable without replacing pack composition or engines.
Costs include metadata authoring, revision policy, migration diagnostics and stronger
conformance tests. Reporting extraction, configuration schema changes and production
qualification remain independently reviewable work. Acceptance of this direction does
not approve those changes, a new registry, or a new execution path.
