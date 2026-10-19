# ADR-0110 — Native workflow applications selected by domain pack and profile

- **Status:** PROPOSED — maintainer review required; no implementation authorized by this record.
- **Date:** 2026-09-23
- **Owner:** Cairn maintainer
- **Epic:** Not filed. Related: #2238 (workflow conformance); #2182 (task executor selection).
- **Scope:** Build-time export of declared pipelines into independently executable customer applications using selected native frameworks. No mandatory `cairn-runtime`.
- **Evidence baseline:** working tree at `3f67d348`, including existing uncommitted changes, inspected 2026-09-23. Source anchors are observations, not clean-release qualification.
- **Proposed amendments:** narrow standalone-export exceptions to ADR-0022 and ADR-0084 D8; an explicit customer-side enforcement boundary supplementing ADR-0028/0034 and ADR-0106. Existing records remain unchanged and authoritative until acceptance.

## Context

The required product is:

```text
Domain pack + Profile + selected declared pipeline + deployment inputs
  → Cairn build/validation
  → native executable application
  → customer infrastructure, independent of Cairn Core
```

The maintainer clarified that the deployed execution framework is selected from the
workload and profile: LangGraph, Temporal, Prefect or Metaflow. A new Cairn orchestration
runtime is not the desired product. Cairn may remain private as an authoring, resolving,
validating and building system. Customer execution must not need the Cairn repository,
the root Cairn distribution, private Core modules, or a reachable Cairn control plane.

This differs from both shipping the existing server image and exporting ungoverned
flow stages. Native export must preserve every feature required by the selected
pipeline, or reject the build. It does not promise that every pipeline works on every
framework, nor that arbitrary Python can be statically understood.

### Observed foundations and limitations

| Evidence | Observation | Implication |
|---|---|---|
| `cairn/cli/commands/export.py:161` | `_render` emits a `cairn.pipeline_file.Pipeline` reference | Existing Python export is not an independent application. |
| `sdk/src/cairn_sdk/compiler.py:248` and `:445` | Generated pack shim and lifted operators import Core | SDK authoring is independent of Core; its generated executable code is not. |
| `cairn/core/engine/compiler.py:92` | `compile_template` builds a graph using Core runtime, operator, expression and service mechanisms | Export needs dependency isolation and semantic preservation, not serialization of an opaque compiled object. |
| `cairn/core/dataflow/export.py:1` and `:100` | Native Prefect/Metaflow stage exporters exist; governance explicitly stays home; gated flows are refused | Reuse the exporters where applicable. Their current contract cannot silently be relabeled fully governed. |
| `cairn/core/operator/protocol.py:187` | `OperationContext` contains injected execution capabilities | Separate selected implementation needs from private ambient platform context. |
| `cairn/core/runtime/checkpointer.py:91` | Existing checkpoint selection uses LangGraph facilities | Reuse native checkpoint semantics; do not invent a new checkpoint engine. |
| `cairn/services/llm/factory.py:97` | LLM construction integrates governance concerns | Export must preserve budget/redaction behavior, including judge calls. |
| `cairn/cli/commands/lock.py:37` | Pack content lock excludes implementation directories | Add executable dependency identities; `cairn.lock` alone is insufficient. |
| `pyproject.toml:443` | Root wheel packages `cairn` | Installing a dependency extra does not remove private modules from the distribution. |
| `Dockerfile.dataflow-task:41`; `deploy/images/train-gpu/Dockerfile:102` | Job images include/install Cairn | Existing job images do not prove Core-free distribution. |
| `tests/core/test_flow_export.py:1` | Export unit tests inspect generated source; target execution is described as separate manual evidence | Add isolated execution qualification, beyond source snapshots. |

### DOCUMENTATION / IMPLEMENTATION CONFLICT and decision constraints

ADR-0022 explicitly rejected a generic IR with multiple backend emitters and states
that Temporal is a durability wrapper, not a second compiler target. ADR-0084 D8 also
rejects new surfaces requiring governance and replay to be re-proven. Full governed
native export introduces such a surface. Reusing existing structures alone does not
remove this conflict.

Native flow generation already exists in `core/dataflow/export.py` under the narrower
ADR-0087 projection contract. This does not authorize an unrestricted replacement
compiler or a claim of governance parity.

ADR-0084 D6 says no Temporal at the pipeline tier, while the repository has a Temporal
pipeline implementation; `docs/adr/README.md` candidate C-5 records that disagreement.
This proposal does not silently resolve it or use it to authorize Temporal-owned ML
step retries. Any exported mixed-target topology must explicitly identify its owner
and receive qualification before support is claimed.

## Decision (proposed)

### D1 — Native frameworks own customer execution

The application runs directly on its selected framework(s). There is no mandatory
`cairn-runtime`, Cairn server, background license callback, private compiler, or hidden
remote execution dependency. A framework's own service, such as Temporal Server, may
be required and must be declared as customer infrastructure.

The absence of Cairn Core is a dependency constraint, not a promise of a single binary
or zero infrastructure. Small distributable libraries for selected implementations,
validation or enforcement are permitted. They must not evolve into a second Cairn
scheduler, generic graph interpreter or orchestration service.

### D2 — Pack describes intent; profile selects compatible execution

Domain packs retain pipelines, flow definitions, schemas, defaults and domain policy.
Operator packs retain implementation code. Deployment profiles select compatible
operator bindings and execution targets; environment wiring supplies credentials,
endpoints and infrastructure configuration. Preserve ADR-0026's distinction between
these concerns. Secret values never become domain-pack data or export defaults.

Build input includes a selected declared pipeline, input schemas or explicit fixtures,
the profile, nonsecret environment requirements and exact installed pack versions.
Pack + profile alone must not imply fabricated input data or credentials. When no
pipeline is selected, enumerate candidates instead of guessing.

No new pack/profile fields, CLI command, entry-point group or public manifest syntax
are finalized here. Their smallest compatible schema delta must be reviewed in the
first implementation plan. Existing bindings should be reused before adding fields.

### D3 — Frameworks have distinct roles and qualified combinations

| Target | Intended responsibility | Restrictions |
|---|---|---|
| LangGraph | Agentic graph/state, conditional execution, interrupts/checkpoints | Not a substitute for arbitrary distributed ML infrastructure. |
| Temporal | Durable workflow/activity orchestration and recovery | External calls belong in activities; replay-safe workflow code; no automatic flattening of agentic or ML engines. |
| Prefect | Selected native data/ML flows and task execution | Own only the retries/state explicitly assigned to it. |
| Metaflow | Selected native data/ML steps, artifacts and resume | Preserve engine-specific persistence/resources and step boundaries. |

These are proposed roles, not a current support matrix. A pure data/ML artifact must
not acquire LangGraph or Temporal without a declared need. A mixed pipeline may use
multiple frameworks only through a qualified composition: one lifecycle owner per
scope, explicit child handles and cancellation/recovery protocols. Mixed-framework
execution is deferred beyond the first single-target proof.

Do not multiply retries across framework boundaries. Specify total attempt limits,
timeout ownership and cancellation propagation. A target unable to express required
behavior must refuse the build.

### D4 — Extend existing compilation; do not invent a universal workflow engine

Reuse `PipelineDef`/planning, `TemplateSpec` and dataflow definitions as the semantic
sources. Export a versioned, immutable projection containing resolved identities,
bindings, requirements and source coordinates. “Workflow IR” means this build artifact,
not a new authoring DSL or a generic runtime interpreter.

Native emitters translate supported constructs into native APIs. Prefer small launchers
and framework-native definitions over copying platform internals into generated files.
Generated code is a derivative, never re-imported as the source of truth. User-owned
operators remain source inputs. Editable generated output is explicitly a fork; its
original conformance evidence no longer applies after edits.

Acceptance of this ADR would permit native export code generation as a bounded
exception to ADR-0022 and permit the export qualification surface despite ADR-0084 D8.
It would not replace Cairn's existing compiler or change its managed execution path.

### D5 — Build must account for every selected construct

The exporter produces a feature inventory with supported, unsupported and unresolved
entries, each pointing to its source construct and selected implementation. Required
unsupported/unresolved entries fail the build. Never silently drop gates, triggers,
loops, retries, conditions, finalizers, resource placement, outputs or policy rules.

Static diagrams show resolved structure and symbolic dynamic behavior. They cannot
claim to discover arbitrary tool internals or predict model-dependent branches.
Diagrams, generated code and manifests must identify the same resolved source plan.
Agreement between those artifacts is necessary but not sufficient proof of correctness.

### D6 — Customer-side enforcement is explicit and shared where feasible

Build-time policy validation is not execution authorization. Each application has an
explicit local admission boundary and protected-effect boundaries, without invoking
the private `launch_pipeline` implementation. This is a proposed scope extension of
the one-door rule, not permission to bypass the door in existing managed Cairn.

Inventory each required guarantee and bind it to a versioned distributable implementation
or a declared customer-hosted service. Prefer extracting a narrow existing implementation
shared by managed Cairn and native applications; do not duplicate policy logic across
emitters. If extraction changes a current Core owner or contract, review that exact
change before implementation. Framework-native features alone do not supply Cairn policy.

| Guarantee | Required exported behavior |
|---|---|
| Admission and policy | Validate inputs, dependencies and authorized policy before effects; record decision identity. |
| Operator/flow approval | Preserve required per-effect and aggregate flow risk checks; refusal performs zero protected effects. |
| Budget/redaction | Cover every selected paid model/tool path, including eval/judge calls; missing enforcement fails closed. |
| HITL | Persist request and decision; authenticate/authorize approver; bind approval to run, action and policy identity; reject replay/stale approval. |
| Quality gates | Failed required evaluation blocks the dependent action; missing evaluator/stub output cannot count as passing proof. |
| Artifacts/lineage | Record pipeline/leg/attempt identity, inputs, versions and produced immutable artifact identities. |
| Cancellation/recovery | Persist handles; define reconciliation for uncertain external effects; do not promise exactly-once effects without provider support. |

Offline artifacts use an explicitly authorized policy snapshot and record its identity.
Define expiry/update rules where the workload requires them; do not invent expiry defaults.
Immediate central revocation is not promised without connectivity. Connected policy
services are permitted only when declared, never as a hidden Cairn dependency.

### D7 — Operators must have a verifiable distribution boundary

Package only selected implementation wheels and dependencies. Existing imports of
`cairn.core.operator`, connections, provider settings, reporting, storage, dispatch or
HTTP internals are extraction blockers for the relevant operator, not automatic approval
to copy those modules into the output.

Prefer independent implementation functions/adapters receiving explicit typed inputs
and narrow injected capabilities. Preserve capability/output/report semantics. No blanket
rewrite of all operators, universal adapter hierarchy or unreviewed new registry is needed.

Runtime interfaces, if distributed, use an independently versioned compatibility
contract. They do not require a package called `cairn-runtime`. Package names and ownership
are decided from the first selected closure, with Core depending on reusable public
implementation components rather than those components importing private Core.

### D8 — Build output is a locked application, not only Python text

Proposed output responsibilities (filenames are illustrative, not accepted APIs):

```text
application/
  manifest.json           # identity, target, features, prerequisites, compatibility
  workflow/               # native definitions, launcher, source mappings
  dependencies.lock       # exact executable dependency identities/hashes
  configuration.example  # nonsecret values and required references
  deployment/             # Dockerfile and selected target manifests
  tests/                  # generated structural tests + independent scenario fixtures
  evidence/               # build provenance, SBOM, qualification results, diagrams
```

Record compiler version, pack/profile digests, operator wheel hashes, target framework
versions, platform/Python/ABI, base-image digest, configuration provenance and asset
digests. Include models/tokenizers/corpora only when explicitly selected and distributable;
otherwise declare immutable references and availability requirements.

Do not serialize credentials, machine-specific paths or private source into artifacts.
Generated commands must not evaluate untrusted configuration as shell code. Builds use
controlled inputs; reproducibility means independently rebuilding the executable closure,
not merely obtaining identical YAML. Model-output determinism is a separate property.

### D9 — Deployment prerequisites and operational behavior are part of export

First target: a standalone Python application in Docker for one qualified framework.
Compose, Helm, GPU variants and offline Linux bundles follow separately. Existing
`Dockerfile.engine`, Compose and Helm assets are reference conventions, not acceptable
Core-free base images by default.

Declare storage/volumes, migrations/init, health/readiness, workers, resource limits,
network access, secret mounting and framework services. Model serving is a separate
declared component where needed. Scheduling requires a selected scheduler and durable
schedule identity; exporting a pipeline does not automatically deploy its triggers.

Air-gapped qualification requires all wheels, system libraries, images and selected
model/data assets locally available, and no runtime download or telemetry requirement.
GPU qualification pins the relevant driver compatibility, CUDA and framework matrix.
No support claim follows from a successful CPU-only build.

### D10 — Existing behavior and compatibility stay explicit

Keep current managed execution and current ungoverned native stage export unchanged
until separately scoped migration work. Do not make an old export command silently
promise governance. Declare artifact-schema, emitter and dependency compatibility;
reject unknown versions rather than guessing.

In-flight executions remain pinned to their original artifact and framework-compatible
state. Upgrade/resume across builds requires explicit checkpoint and policy compatibility
tests. Rollback means selecting a previously qualified artifact; it does not reverse
external effects or automatically downgrade persisted state.

## Verification and acceptance

### Upstream reuse

LangGraph checkpoint/interrupt facilities, Cairn's existing runtime/engine integrations,
native Prefect/Metaflow export and operator wheel packaging are the starting points.
No custom checkpointer, scheduler, graph database or general orchestration engine is
proposed. Framework-version behavior must be checked against the exact locked versions
when each target is implemented; this ADR does not certify upstream feature parity.

### Qualification matrix

| Proof | Required evidence |
|---|---|
| No private dependency | Inspect image/wheel contents, SBOM and transitive imports; fail if private Core is present or imported. No host checkout mounts/editable installs. |
| Execution independence | Clean host executes without Cairn services or credentials; declared framework services remain available. |
| Semantic preservation | Independently specified expected outputs/effects for every selected construct; target-specific negative tests. |
| Governance | Invalid input, denied policy, wrong approver and failed evaluation prevent protected effects. |
| Recovery | Kill/restart around effects and HITL; assert persisted state, reconciliation and bounded attempts; cancellation releases or reports resources. |
| Reproducibility | Two isolated builds compare normalized manifests and locked executable/asset identities; document any non-reproducible packaging metadata. |
| Security | Secret canaries absent from artifacts/logs; permission and network-denial cases; no untrusted-code sandbox claim from graph structure. |
| Portability | Separate qualification per target/version/platform and selected optional integration. |
| Scale/ML quality | Workload limits and quality datasets approved before tests; no production claim from structural conformance. |

Required FAIL or UNKNOWN blocks the corresponding support claim. Tests generated by
the same emitter cannot be the sole oracle. Include a mutation case that intentionally
removes a gate or corrupts a mapping and is rejected by independent expectations.

### Exact first vertical proof

1. Select one declared pipeline and LangGraph profile, a fixed operator set and one
   persistent local checkpoint/artifact configuration. Final selection is an implementation
   planning decision, not permission to add dependencies now.
2. Build privately using existing Cairn resolution; emit native Python and locked dependencies.
3. Execute local-corpus retrieval → local MCP tool → actual local model invocation → policy
   gate before a protected effect → deterministic eval → authenticated HITL → artifact → result.
4. Deploy in a clean container with no Cairn package, source mounts or Cairn network service.
5. Inspect the image and prohibit private imports during tests. Exercise required refusal
   cases and restart while awaiting approval; inspect output/artifact provenance.
6. Repeat with outbound network denied and all selected local services/assets provided.

This proves only that selected agentic configuration. Add a native dataflow export test,
then a training checkpoint/cancellation proof, then durable Temporal and mixed-framework
qualification. A stub can support a focused unit test but cannot establish model, engine,
training or governance integration qualification.

## Slices and stop conditions

| Slice | Deliverable | Exit condition |
|---|---|---|
| S0 — authority and scope | Maintainer decision, selected pipeline/profile, feature inventory, actual transitive closure | Exact conflicts and portable enforcement ownership approved. |
| S1 — independent selected implementations | Minimal operator/integration dependencies and contract tests | No private imports in selected closure; managed behavior preserved. |
| S2 — one native exporter | Existing structures → native application with source mapping | Unsupported required constructs refused; structural tests pass. |
| S3 — enforcement | Portable admission, policy, secrets, HITL and provenance for selected workflow | Independent negative and recovery tests pass. |
| S4 — locked container | Manifest, dependencies/assets, configuration and deployment | Clean-host proof and no-private-code inspection pass. |
| S5 — conformance | Diagram, observed execution and independent expectations | Mutation test catches shared-generation mistakes; evidence report complete. |
| S6 — target expansion | Prefect/Metaflow, training, Temporal, mixed targets, Helm/offline variants as separate scopes | Each combination receives its own support entry and evidence. |

Do not repeat the earlier runtime-extraction estimate: it measured a different design.
Estimate implementation after S0 establishes the first target's dependency closure,
enforcement reuse and accepted contract changes. Avoid a single estimate for all targets.

## Architecture review and invariant impact

- **Affected owners:** engine/export; pack/profile resolution; operator implementations;
  governance; runtime/dispatch; storage/provenance; deployment and tests. No Core-to-pack
  imports, SDK-to-Core imports or low-level-to-HTTP dependencies are newly authorized.
- **Public contracts:** export manifest/versioning and any portable invocation interfaces
  are new proposed surfaces. Changes to `Operation`, `Pack`, `PipelineDef`, `DataflowSpec`,
  `Run` or configuration grammar require exact scoped review. No API/OpenAPI changes here.
- **Managed invariants:** existing launch, gates, LLM factory, run identity and artifact
  registration paths remain unchanged. This documentation edit establishes no new proof.
- **Export invariants:** named Python chokepoints cannot be required at customer runtime
  while prohibiting Core. The proposed exception replaces those location requirements
  with independently tested equivalent guarantees only for qualified native artifacts.
  Acceptance must approve that exception explicitly; it is currently unproven.
- **Twins:** shared implementation changes require managed/export parity tests; applicable
  bridge/Temporal and SQLite/Postgres behavior remains covered. Supporting one export
  backend does not authorize weakening managed twin behavior or existing tests.
- **Risk:** CRITICAL — omitted governance; HIGH — replay/duplicate effects, accidental Core
  disclosure, profile/config drift and incompatible checkpoints; MEDIUM — generated-code
  maintenance and build size. The qualification matrix addresses each, not graph diagrams alone.
- **Recommendation:** PROCEED WITH CONDITIONS as a proposed product direction; implementation
  remains blocked on acceptance of the scoped architecture exceptions and S0 plan.

## Consequences

Benefits: customers operate native frameworks; Core can remain private; deployable
dependencies are inspectable; domains remain separate from execution technology; a
single resolved definition can produce application, diagram and structural evidence.

Costs: each supported target is an ongoing semantic compatibility obligation. Governance
and approval cannot disappear when the central platform does. Selected operators need
dependency work. Offline deployment trades immediate central control for explicitly
versioned local authority. Mixed-framework recovery substantially increases test scope.

Distributing Python wheels or images does not conceal their implementation. Bytecode
or native compilation is not a security boundary. Customer artifacts necessarily expose
some workflow logic, prompts and distributable implementation code. Define which code
may ship; do not claim that an artifact containing executable behavior reveals no IP.

## Alternatives

1. **Deploy existing Cairn images.** Least boundary change and preserves current platform
   behavior; fails the explicit no-Core distribution requirement.
2. **Keep ungoverned native stage export only.** Already has a foundation and fits its narrow
   contract; fails the full workflow/HITL/policy requirement.
3. **Extract a mandatory `cairn-runtime`.** Could centralize behavior but introduces the
   orchestration dependency the maintainer explicitly rejected for this product.
4. **Generate an entire control plane into each application.** Reject: duplicates lifecycle
   and enforcement, increases code volume and makes updates/verification harder.
5. **Call a remote Cairn server from a thin client.** Avoids local Core files but fails
   independent execution and air-gapped operation.

## Open decisions before implementation

- Exact first pipeline/profile and independently distributable operator closure.
- Which enforcement implementations can be shared without creating an orchestration runtime.
- Local approval authority, authentication mechanism and policy update/expiry requirements.
- Minimal export manifest/configuration schema and compatibility policy.
- Which native target combinations are worth supporting, and who owns mixed-tier lifecycle.
- Selected artifact/checkpoint stores and customer-operational responsibilities.

No issue, implementation, release or support-status change is implied by this proposal.

## Dependency graph

```text
PRIVATE BUILD SIDE
  Cairn Core authoring / pack + profile resolution
       → Existing compiler/planners + qualified native emitters
       → Workflow export projection + locked manifest + evidence
                         │
                         ▼
CUSTOMER SIDE
  Native application
       → selected LangGraph / Temporal / Prefect / Metaflow execution
       → selected independent operators + explicit enforcement/integration libraries
       → customer models, tools, secrets, storage and framework services
       → result + checkpoints + artifacts + provenance

BUILD ASSEMBLY
  Workflow export + native definitions + framework/operator dependency closure
       → deployable artifact

FORBIDDEN
  Customer application → private Cairn Core/compiler/control-plane service
  Generated workflow → mandatory Cairn orchestration runtime
```

The graph describes build and execution relationships, not a claim that orchestration
frameworks share one API. Existing Core imports in generated SDK code, operator/runtime
coupling, Core-dependent service configuration and full-Cairn base images remain blockers
until the selected dependency closure and clean-host proofs demonstrate their removal.
