# ADR-0114 — Workflow qualification and evidence-based readiness

- **Status:** PROPOSED — maintainer requested the ADR and implementation tracking; exact evidence format and enforcement rollout require scoped review.
- **Date:** 2026-09-25
- **Owner:** Cairn maintainer
- **Implementation:** #2249
- **Evidence baseline:** local working tree at `3f67d348`; documentation is not deployed enforcement.
- **Preserves:** existing contract owners, ADR-0084 compiler/runtime boundaries, ADR-0103 reports, ADR-0106 declared-pipeline launch, existing governance and storage.
- **Related:** ADR-0111 contracts, ADR-0112 profiles, ADR-0113 bounded agentic components (all proposed); #2238 workflow conformance; #2247 operator inventory/conformance CI; #2248 profile enforcement.

## Context

A schema-valid pipeline can still pass the wrong artifact, bypass a gate, select the wrong
backend or duplicate a remote effect on restart. An operator declaring cancellation or
contract compatibility has not proved that behavior. Generated tests and diagrams can
agree with the same compiler defect. No finite test suite guarantees the absence of bugs.
The aim is explicit, independently checked evidence and bounded support claims.

Current policy already requires layered testing, real handoffs, independent review and
PASS/FAIL/UNKNOWN qualification. See [testing strategy](../testing/strategy.md),
issue workflow and
[verification skill](../../.agents/skills/verify/SKILL.md).
Existing code includes `cairn/core/dataflow/conformance.py`, manifest agreement tests,
architecture boundary tests and profile/trigger/recovery suites. The gap is consistent
requirement coverage and machine-checked evidence selection, not a missing test framework.

## Decision (proposed)

### D1 — Qualify a concrete pipeline and deployment combination

Qualification identifies pipeline/pack/operator/contract revisions, input fixtures,
nonsecret effective profile and policy configuration, runtime/engine/backend versions,
applicable infrastructure assumptions and the tested workload envelope. A pass on builtin
execution is not a pass on Temporal, another provider or a larger workload.

Use the existing compiler/resolved representations to enumerate selected constructs.
Do not introduce a second executable graph or replace #2238's conformance work. Unresolved
references and unsupported features must be explicit. Generated structural cases are
supplemented by author-owned business expectations that regeneration cannot overwrite.

### D2 — Map requirements to evidence

Each affected contract/invariant has its existing identifier or authority section, owner,
applicability rationale, test/qualification reference, expected outcome and result.
Reuse contract identifiers; do not create a parallel registry of contracts or policies.
N/A needs a reviewable reason. Missing installation, optional extras or unrun required
services produce UNKNOWN, never a vacuous PASS.

An evidence artifact records the tested source revision (or reproducible working-tree
snapshot), dependency/definition/configuration identities, commands/check identities,
results and qualification limits. Reference logs/artifacts without exposing secrets or
sensitive input rows. Reuse existing CI artifacts and run-bundle conventions; no evidence
store or new service is required. Exact serialization/versioning is a scoped decision.

### D3 — Independently challenge behavioral claims

Capability conformance tests must cover semantic promises and support envelopes, not only
schema shapes. Include deliberately invalid implementations/requests to demonstrate that
the checks reject ignored options, omitted required results, false cancellation claims,
incorrect artifact identity or prohibited effects. Domain owners supply meaningful expected
results; generated schemas cannot infer correctness of a model, RCA or deployment.

Mocks may replace compute or external transport, never the gate, resolver or handoff under
test. Keep separate evidence for schema/conformance, backend integration and business quality.
A mock provider pass is not a live-backend qualification. Operator coverage remains #2247's
responsibility; full workflow generation/comparison remains #2238's responsibility.

### D4 — Required coverage dimensions

| Surface | Evidence when applicable |
|---|---|
| Contract/operator | Valid and invalid calls, semantic invariants, supported revisions/options, reports, permission/secrets scope and declared lifecycle behavior |
| Flow/pipeline | Actual output-to-input handoff, immutable artifact identity, branches, gates, bounded loops, retries, timeouts and finalizers |
| Profile | Typed merged configuration, inheritance, real consumer selection, unresolved bindings, secret-reference rules and invalid-selection refusal; owned by #2248 |
| Cron/triggers | Timezone/DST, enabled/paused state, catch-up, overlapping runs, event redelivery, inputs and authorization at firing, workspace isolation, update/remove behavior |
| Runtime/infra | Submit/adopt/result, cancellation, worker death, partial persistence, cleanup and remote completion ambiguity |
| Data | Row accounting, schema change, provenance, invalid records, partial outputs and isolation of protected data |
| Training/evaluation | Data/base/candidate/harness identities, split isolation, checkpoint behavior, evaluator failure versus model failure, representative quality/safety slices |
| Inference/batch | Row/output association, duplicate and partial failures, bounded concurrency, real costing and resource envelope |
| Agentic | Typed decision validation, prohibited actions, feedback propagation, termination, malicious observations and aggregate limits; ADR-0113 when implemented |
| Deployment | Actual deployed identity, health checks, rollback/reconciliation and cleanup |

Select checks by actual affected behavior and declared features. Do not demand live GPU,
load or backend qualification for an unrelated documentation edit. Shared-boundary changes
must include affected consumers and runtime/storage twins, not only directly edited files.
A changed selector/check runner must itself have rejecting tests against omitted coverage.

### D5 — Failure and recovery are first-class evidence

Test before submission, after ambiguous remote success, during partial persistence,
during approval/cancellation and after restart. Detect duplicate effects and orphan jobs;
record which system owns idempotency and reconciliation. Do not claim exactly-once effects
because the caller generated a key. Admission and every applicable invocation retain
existing gate/budget/redaction/audit paths, including cron and event-originated launches.

### D6 — Readiness is separate from test results

Each check reports PASS, FAIL or UNKNOWN. Required FAIL/UNKNOWN, missing evidence or a
mismatched qualification context means BLOCKED. Optional unknowns remain explicit and
cannot support a broader claim. READY means the stated scope is qualified, not universal
correctness or unlimited scalability. Independent reviewers examine semantic ownership,
coverage omissions and changed acceptance criteria.

Evidence is reusable only where its identities/assumptions remain compatible. Code,
contract, profile, dependency or environment changes invalidate affected evidence. Use
conservative invalidation when impact cannot be resolved; do not accept a report merely
because its filename exists. Preserve immutable evidence history through existing artifacts.

### D7 — Enforcement stages and authority

Local hooks provide fast feedback. CI validates applicable evidence and publishes gaps.
Required repository checks and protection establish merge blocking; report their actual
configuration separately from local success. Release qualification covers intended runtime
and infrastructure combinations. Existing deployment/admission paths validate current
prerequisites and authority; historical qualification never authorizes a run.

Do not introduce a blanket evidence lookup on every runtime invocation or a new admission
service. Any future runtime requirement to consume qualification artifacts needs an explicit
contract, availability model and compatibility decision. Runtime enforcement continues to
own current permissions, limits, schema validation and audit independently of test reports.

No hidden exemption, widened allowlist or reduced acceptance threshold may create a pass.
Acceptance/enforcement changes require scoped rationale, maintainer review and a rejecting
case. Fixing one check must not silently redefine the product promise.

## Implementation components and existing ownership

| Component | Reuse / owner | New work |
|---|---|---|
| Requirement applicability | Existing contracts, testing strategy and resolved feature inventory (#2238) | Reviewed mapping to required checks; explicit unresolved/N/A behavior |
| Evidence artifact | CI outputs, existing run-bundle conventions | Minimal versioned identity/result manifest; no store |
| Readiness checker | Existing verification/CI workflow | Detect missing, failing, unknown or stale required evidence and emit BLOCKED |
| Conformance producers | #2238, #2247, #2248 and existing operator/runtime tests | Consume their results; do not recreate their harnesses |
| Independent expectations | Domain/contract owners and review | Business assertions and rejecting fixtures independent of generated graph |
| Release/support reporting | Existing support matrix and release procedure | Report tested combinations and limits, never derive readiness from test counts alone |

## Delivery and acceptance

1. Define minimal evidence/applicability contracts using one existing data/ML pipeline and
   one existing authored agentic fixture. No dependency on building adaptive agents first.
2. Add readiness validation with rejection cases: missing requirement, skipped required
   backend, stale operator/profile identity, invalid N/A and altered expected behavior.
3. Integrate with existing checks and CI artifact output; consume #2238/#2247 results as
   they become available rather than claiming unavailable coverage is implemented.
4. Demonstrate a broken wire and removed gate are detected by independent expectations,
   even when generated metadata agrees. Exercise an ambiguous completion scenario.
5. Qualify selected runtime/profile combinations; update docs and support claims. Confirm
   required CI/protection configuration separately before claiming merge enforcement.

Paid/live runs remain individually authorized. No invented throughput or model-quality
thresholds: define workload-specific criteria before qualification. No production code is
changed by this ADR. Existing legacy pipelines are classified with known gaps; do not force
all domain packs to adopt proposed contract grammar or silently label them conformant.

## Alternatives and non-goals

Manual checklists alone are useful but cannot reliably detect stale or absent evidence.
Full-suite pass counts lack requirement coverage. Automatically generated tests alone share
compiler blind spots. Use these as inputs alongside independent semantic expectations.

Excluded: new execution engine, policy evaluator, graph database, contract registry,
certification service, universal test generator, autonomous production approval or a promise
that code can never miss a case. #2238 retains canonical graph/launcher/diagram generation;
#2247 retains all-operator migration closure; #2248 retains profile validation.
