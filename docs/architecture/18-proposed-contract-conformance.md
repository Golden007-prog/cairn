# Proposed capability, artifact, reporting and configuration direction

**DOCUMENTED proposal update (2026-09-24):** this page preserves ADR-0108's historical
proposal, including its pack-owned metadata assumption. The newer [ADR-0111 direction](19-contracts-and-operator-migration.md)
proposes independently discoverable contract distributions instead. Use that page and the
[migration plan](../architecture-fix/operator-contract-migration-plan.md) for current planning;
neither proposal silently changes the frozen baseline. Resolve the carrier decision under
#2230 before production implementation.

**DOCUMENTED / PROPOSED — not CURRENT and not an accepted TARGET.** Decision authority:
[ADR-0108](../decisions/ADR-0108-capability-contract-conformance.md), status PROPOSED.
The existing frozen baseline is unchanged. This page explains
the proposal; it does not adopt it. Evidence snapshot: `3a69f32f`, 2026-09-20.

## Execution shape preserved

**OBSERVED:** declared pipeline launch, governed leg execution and existing contracts
remain the base (`src/cairn/core/dispatch/start.py:179`,
`src/cairn/core/engine/pipeline.py:62`, `src/cairn/core/dispatch/leg_executor.py:114`).
**INFERRED / PROPOSED:** semantic validation enriches that path as described by ADR-0108 D2.

```text
Domain pack definitions + deployment profile
                    ↓
Declared PipelineDef
                    ↓
Existing structural / typed-wire validation
  + proposed capability compatibility checks
                    ↓
Existing policy / admission doors
                    ↓
PipelineRecord → governed leg Runs
                    ↓
     Agentic flow       Data/ML flow         Task leg
        Runtime        DataflowEngine       invoke_operator
           ↓                ↓                    ↓
  operator invocation   stage kernels      operator executor
                    ↓
       Existing artifacts, events and provenance
```

## Changes proposed and their owners

All proposed outcomes below are **INFERRED / PROPOSED**, governed by ADR-0108 D1–D6.
Existing mechanisms are **OBSERVED** at the source anchors in the table.

| Area | Existing mechanism and evidence | Proposed outcome / owner | Explicit limit |
|---|---|---|---|
| Capability | Names and bindings, `src/cairn/core/pack/composition.py:179`; typed wires, `src/cairn/core/engine/pipeline.py:687` | Pack-owned semantic metadata; generic composition checks through current planning/launch boundaries | No new registry, core algorithm enum or selection DSL |
| Data/model handoff | Immutable artifacts with schema and parents, `src/cairn/core/storage/artifacts.py:53`; candidate registration, `src/cairn/core/dispatch/leg_executor.py:436` | Storage identity reused; domain role/format compatibility checked before compute | No duplicate artifact IDs; no dataset rows in control plane |
| Configuration | Flat `TrainSpec`, `cairn/operators/official/training/src/operator_training/schemas/train.py:9`; layering, `src/cairn/core/operator/config.py:114` | Inventory semantic, implementation, artifact, tracking and execution ownership alongside existing scope/precedence | Public-shape changes need a separate decision; governance stays environment-owned |
| Reporting | `Reporter`, `src/cairn/core/operator/reports.py:142`; DOCUMENTED contract in ADR-0103 | Reuse reporting declarations; attach metric applicability and measurement provenance where a concrete need is demonstrated | No parallel report registry; no mandatory eval loss for runs without evaluation |
| Execution | Implemented operator executor set, `src/cairn/core/operator/executor.py:58`; DataflowEngine, `src/cairn/core/dataflow/engine.py:76` | Preserve Runtime / DataflowEngine / operator executor distinctions and profile-selected engines | No universal executor extraction or engine names in semantic pack data |

## Tracking and sequencing

**DOCUMENTED:** #2186 owns training delivery; #2193 owns capability metadata and SFT/reference
validation; #2200/#2201 own preference/DPO contracts; #2191 owns checkpoint semantics;
#2211 owns later selection. Source: their GitHub issue bodies inspected 2026-09-20.
This proposal adds no competing implementation backlog for those outcomes.

**DOCUMENTED:** Epic #2230 tracks this
review's decision, configuration/reporting follow-ups and documentation reconciliation;
it is marked `do-not-build` pending the maintainer decision.

**INFERRED / PROPOSED:** decide ADR-0108 and its additive grammar first, then implement
through those existing issues. Track the remaining configuration/reporting decisions
and documentation reconciliation in a separate review follow-up epic. New implementation
slices require owner-approved scope and evidence; no paid compute is authorized here.

## Conflicts to reconcile, not silently adopt

- **DOCUMENTATION / IMPLEMENTATION CONFLICT:** glossary executor status versus the
  implemented `docker` entry (`03-core-concepts.md:1`;
  `src/cairn/core/operator/executor.py:58`).
- **DOCUMENTATION / IMPLEMENTATION CONFLICT:** historical candidate URI-only gap in
  `training/FREEZE-v1.md:27` versus candidate registration in
  `src/cairn/core/dispatch/leg_executor.py:436`. Preserve the freeze's historical evidence;
  update current coverage separately after a proof.
- **DOCUMENTED status ambiguity:** ADR-0068 and ADR-0070 remain proposed despite
  implemented config mechanisms (`src/cairn/core/operator/config.py:114`). Implementation
  is not evidence of maintainer acceptance.
- **DOCUMENTED sequencing difference:** `training/16-training-planning-and-composition.md:216`
  requires SFT and DPO Phase 1 proofs; #2193 defers DPO to #2201. Final proof needs both.

## Evidence required before readiness

**DOCUMENTED:** ADR-0108's acceptance section specifies refusal, compatibility, governance,
replay, migration and boundary proof. The testing authority remains
[testing strategy](../testing/strategy.md); contract guarantees remain in
[contracts](../contracts/README.md). This page creates no new guarantees or scale limits.
