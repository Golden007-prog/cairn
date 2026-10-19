# Contracts, operators and domain packs — migration direction

> **DOCUMENTED design update, 2026-10-05:** [ADR-0111 production design proposal](../decisions/ADR-0111-contracts-capabilities-and-pack-separation.md#production-design-proposal--2026-10-05-2230) specifies candidate discovery, exact revisions, offline references, authoring and provenance changes. Design preparation is authorized; the schema is still PROPOSED. #2230 acceptance and the explicit P7 review conditions precede production implementation. Gold fixture grammar is not the production format.

> DOCUMENTED 2026-10-01: [Alignment and freeze crosswalk](../architecture-fix/README.md) maps this proposal to #2230 and #2239. Existing frozen authority is unchanged.

**DOCUMENTED / PROPOSED**, 2026-09-24. Design: [ADR-0111](../decisions/ADR-0111-contracts-capabilities-and-pack-separation.md).
It is not yet an accepted production schema. **DOCUMENTED maintainer scope:** update
architecture guidance and file implementation work; migrate **all operators**, but only
selected domain packs. This scope does not authorize an unreviewed public-contract change.
The [migration plan](../architecture-fix/operator-contract-migration-plan.md) and its
inventory define the work and evidence. Existing managed execution remains authoritative.

## Read before changing these surfaces

**DOCUMENTED review requirement:** for operator, capability, pack, schema, configuration
or reporting work, record the ADR-0111 impact and migration status before implementation.
Use the rules below for the proposed destination; preserve legacy behavior until a scoped
plan is approved. If a change conflicts with the destination, explain the conflict and
obtain a scoped decision rather than introducing another pattern. Do not implement gold
fixture syntax as production grammar without its own schema/compatibility review.

These are review obligations, not a claim that CI already enforces the new contracts.
The [issue workflow](17-issue-workflow.md) governs approval and evidence. Technical
enforcement must arrive through the tracked rejecting tests and CI closure gate.

## Ownership and dependency direction

All destination statements below are **PROPOSED** under ADR-0111 D1–D11.

| Owner | Owns | Excludes |
|---|---|---|
| Independently discoverable contract distribution | Capability identity/revision, shared data/config/result schemas, semantic report definitions, obligations, compatibility and examples | Domain workflows, handler code, providers, secret values, artifact stores |
| Domain pack | Declared pipelines, flows, capability-only mappings, composition-specific schemas, policy intent, presets and examples | Concrete operator/provider choice, implementation code, duplicated shared meaning |
| Operator distribution | One capability identity per operator, supported revisions/options, adapters, implementation schemas/config/diagnostics, tests | Workflow ordering, private dispatch/HTTP imports, altered shared semantics |
| Existing platform/profile/environment | Generic validation, profile bindings, configuration precedence, execution selection, authorization, secrets and persistence | Hardcoded domain algorithms or another contract-specific runtime |

```text
domain packs ── require ──► independent contract definitions
                                   ▲
operator implementations ───────────┘
profile → compatible operator binding → existing governed invocation
```

**PROPOSED:** one operator package may contain many operators; each operator exposes
one semantic capability identity, potentially multiple compatible revisions. Domain
schemas reference shared contracts and exist locally only for a new composition shape.
Operation files are simple named mappings where useful; any inline form must lower
through the same mechanism. Domain executable dependencies reference capabilities;
references to local flows, shared schemas and logical resources remain valid.

**PROPOSED:** semantic configuration/report meaning is shared; implementation-specific
options and diagnostics remain operator-owned. Preserve current precedence and governance
scope. Reuse ReportSpec and manifest agreement. Required quality evidence is a typed
result/artifact, not best-effort telemetry. Schema validation does not prove data integrity.

## Current evidence and limits

| Classification | Evidence | Consequence |
|---|---|---|
| OBSERVED | `src/cairn/core/operator/protocol.py:304` supports capability lists | Exactly-one capability requires compatibility migration, not a blanket metadata edit. |
| OBSERVED | `src/cairn/core/pack/composition.py:179` already validates contributions/bindings | Extend existing discovery/composition; no parallel registry service. |
| OBSERVED | `src/cairn/core/operator/config.py:114`; `src/cairn/core/operator/executor.py:113` | Reuse config/executor machinery. |
| OBSERVED | `cairn/packs/training/operations/run-train.yaml:6` binds a concrete operator | Legacy callers need adapters/profile mapping while selected packs migrate. |
| DOCUMENTED | ADR-0103, ADR-0106 | Preserve operator reporting and declared-pipeline launch. |
| DOCUMENTATION / IMPLEMENTATION CONFLICT | ADR-0068/0070 remain labelled Proposed although their mechanisms exist | Do not report them as accepted ADRs or silently change authority. |

**OBSERVED inventory**, local working tree `3f67d348`, 2026-09-24: 112 operator
identities across 37 on-disk operator packages; manifest names, operator entry points
and literal registration names agree. This is not installed-package or production proof.
See the [operator ledger](../architecture-fix/operator-contract-migration-inventory.csv).
Another 33 stage/engine entry-point entries are separately inventoried; they are not
automatically Operation implementations. Classification prevents them being skipped or
silently forced through an incompatible interface.

## Migration and future-agent obligations

**DOCUMENTED maintainer scope / PROPOSED delivery rules:**

1. Every operator is in scope, including builtins, infrastructure/service operators,
   optional or currently undiscovered packages, and test doubles. No domain opt-out
   exempts the operators it uses. Third-party installed extensions require compatibility
   classification rather than pretending local source inventory includes them.
2. Every newly added operator enters the ledger and target conformance checks. Existing
   operators move through reviewed package-sized slices. Do not add permanent legacy
   exemptions, hide failures or delete an operator simply to reach 100%.
3. Domain-pack adoption is explicit and selective. An untouched pack is LEGACY-COMPATIBLE,
   not semantically certified. Test its calls against migrated operators; retain a bounded
   compatibility adapter rather than rewriting all domain packs.
4. Record schema owner, capability revision, supported options, input/output/report
   validation, config precedence, governance path and compatibility evidence for each
   operator. Metadata-only changes do not constitute migration.
5. Preserve launch/gates/budget/redaction/audit/run and artifact identity; test applicable
   task/template, bridge/Temporal and storage twins. Do not bypass failed requirements.
6. Known runtime defects retain their issue owners (#2182/#1488); migration must not claim
   those paths conform until their evidence passes. Refuse unsupported combinations.

The gold prototype is illustrative and uninstalled. Its passing isolated checks do not
prove production discovery, real training, policy enforcement or all-operator migration.
See [gold review](../architecture-fix/gold-training-review/README.md).

## Decisions and tracker boundaries

**DOCUMENTED:** #2230 owns the architecture decision gate; ADR-0111 proposes a different
distribution direction from ADR-0108. #2193 retains training-specific semantic composition
work; #1107 retains invocation-authoring work but its older concrete-operator examples
need reconciliation before adoption. #2238 owns broader workflow conformance. The new
all-operator migration epic links them; it does not recreate their deliverables.

The [migration plan](../architecture-fix/operator-contract-migration-plan.md) contains
current issue links, wave dependencies, rollback and completion evidence.

**DOCUMENTED profile-scope review:** consult [ADR-0112](../decisions/ADR-0112-profile-scope-and-enforcement.md) before changing deployment configuration. The proposed typed scope and compatibility requirements complement operator migration; they are not yet runtime enforcement.
