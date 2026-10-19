# ADR-0116 — Reproducible execution, efficient workers and optional ClearML integration

- **Status:** PROPOSED — maintainer requested documentation and tracking, not implementation approval.
- **Date:** 2026-10-01
- **Owner:** Cairn maintainer
- **Tracking:** #2267
- **Evidence baseline:** local working tree at `3f67d348`; official ClearML documentation reviewed 2026-10-01. Source inspection, not live integration or scale qualification.
- **Preserves:** ADR-0020/0026 pack and dependency boundaries; ADR-0081 artifact ownership; ADR-0084 compiler/runtime ownership; ADR-0102/0103 reporting; ADR-0104 remote deployment; ADR-0106 declared-pipeline launch.
- **Related proposals:** ADR-0111 contracts, ADR-0112 profiles, ADR-0114 qualification and ADR-0115 registration. None is accepted by this ADR.
- **Impact:** HIGH for any public provenance, cache or lifecycle contract change. Exact deltas require scoped approval. No production schema is specified here.

## Context and evidence

ClearML separates SDK instrumentation, a metadata/API/UI server, execution queues and
agents; data management and serving are additional components. Its Kubernetes integration
maps queued tasks to Jobs. Pipeline controllers coordinate tasks and can reuse cached step
outputs. These are useful operational patterns, not proof that Cairn needs another platform.
Official sources and edition caveats are listed below.

Observed Cairn seams:

| Existing owner | Inspected evidence | Implication |
|---|---|---|
| Governed invocation | `cairn/core/operator/runtime.py:888,950` | Preserve the public invoke path and approval enforcement |
| External job lifecycle | `cairn/core/operator/jobs.py:85` | Reuse submit/poll/cancel; terminal output is returned through poll |
| Remote event transport | `cairn/core/operator/jobs.py:96` | Existing run-scoped live reporting supports NATS or HTTP; no new mandatory broker |
| Artifact identity/lineage | `cairn/core/storage/artifacts.py:1,54,75` | Immutable content-addressed artifacts; registration is control-plane-owned |
| Tracking selection | `cairn/operators/official/training/src/operator_training/schemas/train.py:79` | Current training options are MLflow/W&B; ClearML would be an explicit extension |
| Optimization/runtime ecosystem | `pyproject.toml:213,226,234` | Temporal, Optuna and NATS already have dependency surfaces; do not reproduce their mechanisms |

No ClearML reference was found in the inspected `cairn/`, `pyproject.toml` or `uv.lock`.
This establishes no existing named integration, not absence of generic interoperability.
The support matrix remains the source for support claims. No tests or live workloads were
run to certify the proposed behavior.

GitHub revalidation found #28, #2265 and #2261 open; #1622 (trainer fingerprint) and #145
(sweep/compare) closed. Their descriptions are historical evidence to recheck, not proof
that those features are missing or fully qualified today.

## Proposed decisions

### D1 — Adopt patterns through existing owners; keep ClearML optional

Cairn remains responsible for declared pipeline admission, policy, run identity, lifecycle
coordination and canonical artifact registration. Installed operator/backend integrations
may use ClearML. No core import of a provider pack, mandatory ClearML SDK/server, parallel
registry, new workflow DSL or second launch door is introduced.

Reuse LangGraph and selected durable runtimes for their existing orchestration duties.
Use the existing remote-job boundary for external tasks. Do not add a universal executor
or another queue service merely to mirror ClearML's vocabulary. A ClearML agent is an
execution worker, not an adaptive LLM agent.

### D2 — Make execution provenance resolvable and honest

Inventory existing provenance first. Fill demonstrated gaps through current Run,
PipelineRecord, artifact and report owners. Desired evidence includes:

- Definition revision and resolved contract/operator package versions.
- Effective nonsecret configuration with its source/precedence.
- Immutable input dataset/model identities, including resolved versions behind mutable aliases.
- Code revision and approved build/package identity; image digest or resolved environment fingerprint.
- Runtime/backend and external job identity, correlated to the Cairn run and attempt.

Define which identities are known at admission and which execution facts are appended by
the backend; never fabricate a fingerprint before environment construction. Pin admitted
intent and preserve actual attempt evidence across resume. Do not silently modify historical
rows or introduce a second provenance store. SQLite/Postgres and old-row hydration must
remain aligned if an approved storage change is needed.

An environment fingerprint improves reproducibility but does not guarantee bitwise identical
training. Report separately whether inputs/configuration were reproduced, environment was
reproduced and numerical results were repeatable under the qualified workload.

### D3 — Improve worker visibility and bounded capacity without moving ownership

Extend the existing worker/execution owners only after measuring current behavior. Expose
queue delay, capability/resource mismatch, setup/download time, execution time, heartbeat
and cancellation state where those facts are available. Unknown reasons stay unknown.

Separate control-plane/controller capacity from expensive training, data processing and
inference. Resource selection is deployment/backend configuration; domains retain semantic
requirements rather than concrete queue names or Kubernetes templates. Avoid nested retry
ownership: the run/attempt and backend attempt relationship must be explicit.

#2265 owns capacity/isolation/backpressure decisions. ClearML's worker pattern does not
prove Cairn lease, fencing, fairness or recovery guarantees and does not replace their tests.

### D4 — Separate three kinds of caching

| Cache | Owner and proposed rule |
|---|---|
| Environment / dependency cache | Backend deployment; immutable image/lock identity, safe concurrent reuse and bounded disk use; coordinate #28 |
| Downloaded artifact bytes | Worker/backend materialization; immutable identity, integrity check, workspace/authorization isolation, eviction and corruption recovery |
| Completed computation outputs | Explicit opt-in execution semantics, requiring a separately accepted cache-validity contract |

Do not create one generic cache service. Investigate existing implementations and measure
cold/warm cost before adding code. Dataset fingerprint deduplication is not automatically a
semantic result cache.

Result-cache eligibility must account for implementation and contract revision, canonical
inputs/configuration, input digests and relevant environment identity. Mutable external
state and nondeterminism require explicit handling; default to no result reuse for writes,
deployments, uncontrolled external queries or stochastic work without an accepted policy.

A cache hit must recheck current admission/authorization, verify outputs still exist and are
valid, record the source execution and cache-hit outcome, and never represent a fresh
execution or approval. Dataset/model revocation, cross-workspace access, changed policy,
corrupt bytes and deleted artifacts must refuse reuse. Cache access cannot bypass gates.

### D5 — Improve experiment comparison through semantic reports

Reuse ReportSpec/report ingestion, artifact identity and existing sweep/optimization work.
Compare metrics only when dataset split/version, objective, units, aggregation and evaluation
context are compatible. Missing metrics remain unknown, not zero. Infrastructure diagnostics
remain distinguishable from capability metrics. Candidate selection does not imply promotion.

Reuse Optuna where appropriate; do not adopt ClearML merely to obtain a search algorithm.
Bound trial count/concurrency/time and apply existing cost/policy owners to each trial and
the campaign. Revalidate #145 and #1622 before filing residual feature work. Coordinate
write-volume improvements with #2261, not a second telemetry ingestion system.

### D6 — Evaluate optional integration in stages

1. **Experiment tracking first, if there is an actual user need.** Explicitly export an
   allowlist of configuration, metrics and artifact references. Keep ClearML dependencies
   optional in the integration distribution; do not initialize automatic capture globally.
   Required tracking must fail explicitly when unavailable; best-effort tracking must expose
   degraded delivery without losing Cairn's authoritative reports. Decide that policy before use.
2. **Remote task execution only for an existing/justified ClearML deployment.** Adapt the
   existing submit/poll/cancel protocol and preserve Cairn run-to-external-task correlation.
3. **Dataset and serving adapters deferred until demand.** ClearML dataset/model IDs are
   external references, never replacements for canonical Cairn identities. Do not create
   serving engines, dataset registries or controllers that already exist elsewhere.

Proposed execution shape, not implemented:

```text
Declared Cairn pipeline → existing admission / governed invocation
  → optional provider integration → ClearML task / queue / worker or K8s Job
  → status + reports + artifact descriptors
  → Cairn contract validation and control-plane artifact registration
```

Secrets stay in scoped deployment connections; domain definitions do not acquire provider
credentials or concrete queue wiring. Exact binding syntax awaits the relevant approved
profile/contract decision. A metadata-export integration needs separate scope from an
operator that performs remote execution; do not force telemetry into a new public capability.

### D7 — One lifecycle authority for each responsibility

For remote integration, document responsibility for submit identity, retry, cancellation,
worker recovery and terminalization. An accepted-but-response-lost submission must be
reconciled through stable correlation or reported as ambiguous; never blindly resubmit and
claim exactly-once execution. Revalidate whether the provider offers the needed lookup.

Test restart after submission, cancellation during setup/execution, late/out-of-order events,
provider outage, missing outputs and failures after an external task reports completion.
External success is insufficient: Cairn validates required outputs/contracts before success.
ClearML controllers must not independently schedule a second copy of the Cairn graph.
Wrapping an opaque external pipeline would require separate lifecycle/visibility decisions.

## Alternatives and tradeoffs

- **Keep current behavior:** lowest maintenance cost; appropriate wherever existing evidence
  already satisfies the use case. Accepted as the default for unproven gaps.
- **Selective improvements and optional adapters (recommended):** preserves modularity and
  customer choice, but requires lifecycle mapping and compatibility testing when integrated.
- **Mandatory ClearML platform:** gains a unified external experience but adds operations,
  competing identities/controllers and vendor coupling; not recommended.
- **Build equivalent platform services in Cairn:** rejects reuse and expands maintenance;
  not recommended. Existing runtimes, artifact stores, Optuna and reporting remain owners.

## Delivery and decision gates

| Slice | Tracking / disposition |
|---|---|
| Accept/revise/reject this proposal; exact deltas and uncovered work | #2267 |
| Independent contracts and conformance | #2230 / #2239 / #2240–#2247; no implicit acceptance |
| Dependency/environment cache | #28; revalidate historical executor assumptions |
| Worker capacity/isolation/visibility | #2265; qualify workload before adding limits |
| Durable report batching | #2261 |
| Provenance and experiment comparison gaps | #2267 revalidates closed #1622/#145 and routes only residual work |
| Artifact materialization/result-cache validity | #2267 decision scope; separate implementation issue only after reuse/ownership review |
| Optional ClearML reporting/execution | #2267 demand and lifecycle review; deferred implementation |
| Profile and qualification boundaries | #2248 / #2249; configuration consolidation stays #2266 |

Each accepted slice needs an existing delivery owner or a scoped follow-up issue before
implementation. This ADR does not make all slices prerequisites for unrelated work or
rewrite the frozen training roadmap. Contract-changing work follows the alignment index.

**Cross-verify these issues before doing any fix.** Recheck code, accepted contracts, current
issues/PRs, dependencies and measurements; narrow or close stale findings rather than build
the described solution automatically. Maintainer approval remains required for architectural
deltas. Reviewers supply evidence, not approval.

## Qualification and consequences

- Negative evidence: refused cache reuse after revoked access or changed policy; corrupted
  artifacts rejected; incompatible metric comparisons refused; telemetry secrets excluded.
- Lifecycle evidence: lost submission response, restart, repeated poll, cancellation and
  late events preserve run identity and cannot duplicate protected effects silently.
- Twins: applicable bridge/Temporal paths, SQLite/Postgres records, manifest/class metadata
  and API/OpenAPI/console representations remain aligned for the selected slice.
- Performance: measure cold/warm environment setup, download bytes/time, cache hit ratio,
  queue p50/p95 delay, throughput at bounded concurrency, report intake/storage cost and
  cancellation/recovery time. Define workload and acceptance thresholds before experiments;
  no invented throughput or production-readiness guarantee.
- Cost: optional integrations add server/agent deployment, upgrades and dependency testing.
  Confirm versions, licenses and feature editions for each deployed component. No paid/live
  validation is authorized by this ADR.

Required FAIL/UNKNOWN evidence blocks readiness for the affected slice. Update the support
matrix and external documentation only after qualification; record any remaining limitation.

## Official research sources

Reviewed 2026-10-01; vendor documentation describes features, not Cairn integration proof.

- [ClearML repository](https://github.com/clearml/clearml): SDK and related modules; component licenses must be checked individually.
- [Architecture](https://clear.ml/docs/latest/docs/getting_started/architecture/): SDK/server/agent/data/serving separation.
- [Workers and queues](https://clear.ml/docs/latest/docs/fundamentals/agents_and_queues/): pull execution, environment construction, worker modes.
- [Kubernetes](https://clear.ml/docs/latest/docs/clearml_agent/clearml_agent_deployment_k8s/): queue-to-Job mapping; advanced enterprise features are distinguished.
- [Pipelines](https://clear.ml/docs/latest/docs/pipelines/): controller tasks and opt-in step caching.
- [Data](https://clear.ml/docs/latest/docs/clearml_data/): file-based dataset versions, parents and materialization.
- [Data cache](https://clear.ml/docs/latest/docs/clearml_agent/clearml_agent_data_caching/): downloaded-file caching.
- [Hyper-Datasets](https://clear.ml/docs/latest/docs/hyperdatasets/dataset/): enterprise SDK distinction; not assumed available in OSS.
- [Optimization](https://clear.ml/docs/latest/docs/clearml_sdk/hpo_sdk/): bounded HPO and Optuna integration.
- [Serving](https://clear.ml/docs/latest/docs/clearml_serving/): endpoint/engine/statistics separation. Online request batching is not offline batch processing.
