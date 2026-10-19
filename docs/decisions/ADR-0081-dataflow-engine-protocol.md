# ADR-0081 — The dataflow extension: an engine-agnostic data plane behind operators, backends, and pack dataflows

Status: Proposed (2026-07-20)

Relates to / builds on: **ADR-0080** (dataset data-plane separation — diagnosed
the control/data split and shipped its vocabulary; this ADR **completes** it by
moving data execution out of the workflow engine entirely), ADR-0078 (columnar
data plane), ADR-0079 (PII cascade + measured throughput), **ADR-0046 + #302**
(profile *backends* model — the attachment mechanism this ADR extends),
**ADR-0070** (governance vs operational config split — the authority model for
who picks what), ADR-0069/#746 + #565 (remote-job spine + dedicated-queue
routing; also the **bundle pipeline** concept this ADR deliberately does NOT
collide with — see §2 Naming), #989 (run lineage), **#1031 / PR #1044**
(`fix-remote` — the live-proven rung-1 seed of the engine described here),
#1037 (job-machine deploy — subsumed), #962 (Shardable — superseded).

---

## 1. Context — the evidence that the workflow engine is the wrong data substrate

cairn's dataset workflows were built with their processing stages as **graph
nodes**: `scan_all`, `dedup_apply`, `decontam_apply`, `anonymize` are operators
executing *inside* the workflow runtime (in-process or as Temporal activities
on the shared worker). Over the 2026-07 epic (#1027), every scaling step turned
out to be a **workaround against the execution substrate**, not a feature of
it:

| Fix we shipped | What it was actually working around |
|---|---|
| Fused single-pass check (P1b, `scan_all`) | Per-node re-streaming — node granularity ≠ data granularity |
| Payload lint + sample caps (P4, D6/R5) | Workflow state cannot carry data — ~2 MB history limits |
| Two live control-box wedges (Presidio saturation) | Data work co-located with the control plane starves it |
| `fix` / `fix-remote` / `fix-batch` — THREE variants of one repair | No general boundary to the data plane, so each escape hatch was bespoke |
| Blanket 1800 s IO timeout (#1023) | One monolithic activity for a whole corpus pass |

Meanwhile **training never had this problem** — by accident of hardware.
Nobody writes the torch loop as LangGraph nodes: `train/runpod`,
`remote/exec → cairn-train-local` submit a job to a machine that owns its
cores, poll for scalars, and resume. The graph node is a **trigger and a
governance boundary**; the trainer is its own program. That pattern has been
live-proven repeatedly (#564, #565, #601/#603, and again on 2026-07-20 with
`fix-remote`).

The generalization: every heavy workload cairn touches has the same signature —

> **references to data artifacts in → new artifacts + scalar stats out, with a
> lifecycle (submit → progress → terminal) in between.**

Dataset prep, LLM batch inference (synthesis, judge-at-scale, embeddings),
logs/trace analytics, eval harnesses, and training itself all fit it. Each has
a different natural engine; all share one boundary shape. ADR-0080 built the
vocabulary for this split (queues, job machines, scalars-only state) but left
the stages as workflow-engine citizens. This ADR removes them.

Structural reasons the workflow engine can never be the data executor (not
fixable by tuning):

1. **Node ≠ shard.** A 10k-shard fan-out cannot be expressed as activities —
   event-history counts and payload limits are hard walls; the scheduler has
   no data locality, no shuffle, no map-reduce primitive.
2. **Graph state is a merged dict**, antithetical to columnar streaming; the
   D6 payload lint is a guard against a category error, not a cure.
3. **Replay-determinism governs decision history, not artifacts** — the engine
   cannot content-address, cache, or resume *data* work at sub-node
   granularity.

---

## 2. Naming — why "dataflow", not "pipeline"

"Pipeline" is already **doubly booked** in cairn:

- **Bundle pipelines** (ADR-0069): `[[bundle.pipeline]]`, `PipelineBridge`,
  `pipeline_store`, `pl_*` ids, `POST /pipelines/run`, and the console
  **Pipelines** view — there, a pipeline is a *chain of governed workflow
  runs* (check → prep → finetune).
- **`ctx.pipeline_id`** on `OperationContext` — legacy naming for the run id.

Introducing a third meaning ("stage list executed by a data engine") into
packs, operators, and the console would be a permanent vocabulary fracture.
The new concept is therefore named **dataflow**, which is also the more
accurate word: the ADR's own core distinction is that *a template declares
control flow; a dataflow declares data flow* (precedent: Google Cloud
Dataflow — exactly this kind of engine-executed stage DAG).

Resulting vocabulary, side by side:

| Term | Meaning | Surface |
|---|---|---|
| workflow / template | control flow — gates, HITL, synthesis | packs `templates/`, console Workflows |
| **pipeline** (unchanged, ADR-0069) | a chain of workflow **runs** | bundles, console Pipelines |
| **dataflow** (new, this ADR) | data flow — stages over shards on an engine | packs `dataflows/`, console Dataflows |

---

## 3. Decision

Five decisions, D1–D5. Together: **cairn stays the decision plane; all data
execution moves behind one operator, speaking one protocol, to engines
attached as environment backends.**

### D1 — All custom execution stays operator-mediated. No bypassing.

The **only** door from a workflow to the data plane is an operator:
`dataflow/run`, a `remote_job`-executor operator exactly like `train/runpod`.
Neither templates, nor the server API, nor console actions may invoke an
engine directly.

Why this is load-bearing and not ceremony — every governance property cairn
has attaches **at the operator boundary**, and the data plane must inherit all
of them for free:

- **Compile-time risk gating**: `dataflow/run` carries `external_write` risk
  (derived from its stages — see §7); the compiler forces a checkpointer +
  HITL gate on any template that binds it. Structural safety, not convention.
- **HITL / approvals**: one gate approves the *whole* submitted dataflow (the
  `fix-batch` model: one job, one approval).
- **Audit + lineage**: the run/operation audit chain records the submission,
  the approver, and the result — unchanged machinery.
- **Policy + capabilities**: engine handles resolve through the operator
  context (like `ctx.compute`), never ambient credentials.
- **Mock/sandbox**: `supports_mock` gives the Playground a data plane for
  free.
- **Queue routing**: `executors=["remote_job"]` routes to `QUEUE_REMOTE`
  as today (#565) — no new dispatch machinery.

### D2 — A new dataflow core, and a dataflow concept in packs.

**Core** (`cairn/core/dataflow/`) gains the *contract* — small, typed,
dependency-free:

```
DataflowSpec    dataflow_ref | inline stages · input_artifacts[] · config
                · budget {max_cost, max_rows, max_seconds} · parent_run · engine hint
StageRef        stage (registry name) · config (validated against the stage's schema)
DataflowResult  output_artifacts[] · stats {scalars} · cost · warnings · lineage
Manifest        shard URIs · schema · per-shard write-time stats · content hash
```

**Core** also gains the **artifact registry**
(`cairn/core/storage/artifacts.py`, Postgres + SQLite backends mirroring
`fingerprints.py`):

```
artifacts         id (content hash) · kind (dataset|model|report|index) · uri
                  · schema · stats · size · producer_run_id · account_id
artifact_parents  child → parent            (the lineage DAG)
artifact_tags     name → artifact_id        (the ONLY mutable thing)
```

Semantics: versions are **immutable and content-addressed**; stats are frozen
at write time (a gate judges a dataset without reading it); **tags** are
mutable pointers and **tag promotion is where HITL attaches** ("may v41 become
`@prod`" — the same shape as model-registry stage promotion). The registry
absorbs three existing fragments: the training artifact store, the
fingerprint index (stays as its own table), and the ad-hoc `fixed_path`
relaying between check/fix/re-check.

**Packs** gain `dataflows/` as a first-class **data** artifact kind — the
declarative mirror of the architectural split itself: a *template* declares
control flow (gates, HITL, synthesis); a *dataflow* declares data flow (stages
over shards). They meet at exactly one point: an operation binding
`dataflow/run` that names the dataflow.

```yaml
# packs/<domain>/dataflows/clean.yaml       — DATA, in the domain pack
name: clean
input: dataset
stages:
  - stage: dedup.exact
    config: { normalize: true }
  - stage: decontam
    config: { eval_ref: $input.eval_ref }
  - stage: pii.redact
    config: { preset: $env.pii_preset }     # governance knobs stay env-owned (ADR-0070)
  - stage: export.parquet
outputs: [cleaned]
```

Phase-in: **v1 needs no pack-contract change** — the stage list may ride
inline in the operation binding's input (the engine does not care where the
spec came from). It graduates to first-class `dataflows/` in the same phase
that adds stage **risk classes**, because that is when the compiler needs to
*read* the spec (to aggregate dataflow risk → forced gates) and `cairn
validate`/doctor/lock need to check stage refs and config schemas. First-class
also buys addressability (`<pack>/<dataflow>`), console listing, and
`cairn.lock` hashing — the point where the new artifact kind pays for its
contract cost.

Stage *implementations* are code and therefore live in **stage/operator
packs** (entry-point group `cairn.dataflow_stages`), never in domain packs —
the packs-are-data rule is unchanged.

### D3 — The dataflow engine is a PROTOCOL in core.

`cairn/core/dataflow/engine.py` defines the engine-agnostic protocol —
the same pattern as `RemoteJobChannel` and the storage backends:

```python
class DataflowEngine(Protocol):
    name: str
    def validate(self, spec: DataflowSpec) -> list[str]: ...
    async def submit(self, spec: DataflowSpec, ctx) -> EngineJobHandle: ...
    async def poll(self, handle) -> EngineJobStatus:      # state · progress
        ...                                               # metrics · partial stats
    async def cancel(self, handle) -> None: ...
    async def result(self, handle) -> DataflowResult: ...
```

Hard rules of the protocol:

- It speaks **manifests and scalars, never rows**. Engines own all execution
  semantics — sharding, distribution, shuffles, caching, resume — invisibly
  behind it.
- `poll` streams **progress metrics** (stage, rows done, spend). This is the
  generalization of the training learning-gate: mid-run **budget gates**
  (abort at $N) and **quality gates** read the same stream via the existing
  control-loop early-abort path (#552 machinery, unchanged).
- Artifact **registration happens on the control-plane side** from the
  returned descriptors — job machines and clusters get **no registry/database
  credentials** (a boundary we hit live: fingerprint publish from a box with
  no DB access).
- **The conformance suite IS the protocol** (Beam's `ValidatesRunner`
  pattern): core ships `tests/dataflow_conformance/` — one pytest suite
  parameterized over engines (submit→result round-trip, cancel-kills-compute,
  resume-after-kill, metrics-stream shape, descriptor validity, budget
  abort). An engine *is* whatever passes it; builtin must pass in Phase 1,
  every adapter runs the identical suite. Deviation becomes mechanically
  detectable, not a review opinion.
- **Engine-neutral spec; engine config quarantined.** `DataflowSpec` contains
  zero engine-aware fields; anything Prefect/Dagster/Ray-specific (work pool,
  executor, runtime_env) lives only under
  `[profile.backends.dataflow.<engine>]`. A field wanting into the spec
  "because engine X needs it" is the drift alarm.
- **Capability discovery, not lowest-common-denominator** (Beam's mistake,
  inverted): `engine.validate(spec)` reports what *this* engine cannot run;
  doctor surfaces it preflight. The spec never shrinks to the intersection of
  all engines.

Implementations conform to the protocol; cairn treats them identically:

| Engine | What it is | When |
|---|---|---|
| `builtin` | The reference runner: a console-script executable (generalized from `run_fix_pipeline` + `fix_entry`, live-proven 2026-07-20) — spec interpreter, per-shard streaming executor, DuckDB-style global state, `.done` resume markers, heartbeat reporter. Dispatched to one machine via the existing SSH launcher (`remote/exec` machinery) or a k8s Job. | Phase 1 — covers all of "medium" |
| `ray` | Adapter submitting the same spec to Ray (Ray Data execution; KubeRay on k8s). Stages expose per-batch **kernels** so the cluster owns the loop. **The capability adapter** — the only engine that adds data-plane execution power (streaming, shuffles, actor pools); Prefect/Dagster add orchestration convenience around kernels we ship regardless. | Phase 3 — the scale rung |
| `prefect` | A bridge-deployment adapter: REST `create_flow_run` with the spec as parameters; a **runtime-dynamic** bridge flow interprets stages via the same kernel entry-points. **The first orchestrator adapter to build** — cheapest conformer: REST-native parameterized runs, no static-DAG contortions, lightest self-host for CI conformance tests. | Phase 3 — after Ray |
| `dagster` (optional) | A Pipes-style adapter (static bridge job + DynamicOutput fan-out) for shops that already run Dagster; buys dual visibility (cairn artifacts as materializations in *their* asset graph). Dagster is **not** embedded (see R2) — cairn talks to it exactly as to any other engine. | Demand-driven — build when a design partner runs Dagster |
| future: `llm-batch`, `analytics` | Provider batch-API drainers; windowed OLAP over logs/traces (watch-triggered). Same protocol. | Phase 3+ |

The builtin engine deliberately has **no scheduler, no UI, no cron, no run
database** — cairn owns all of that. It is an *executor*, runnable standalone
from a shell (which is what makes it testable).

### D4 — Engines attach as environment BACKENDS.

The attachment mechanism is the existing profile-backends model (ADR-0046,
#302) — engines are provisioned infrastructure, exactly like storage, RAG
stores, and compute connections:

```toml
# profiles/production.toml
[profile.backends.dataflow]
engine  = "builtin"                  # builtin | ray | dagster | …
target  = "connection:jobbox-ssh"    # a compute.ssh connection (rung 1)
# engine = "ray"
# target = "ray://kuberay-head:10001"

[profile.backends.dataflow.limits]
max_concurrent_jobs = 4
default_budget_usd  = 25
```

The resolver injects the configured engine into `dataflow/run` through the
operator context (the `ctx.compute` pattern) — the operator never constructs
an engine itself.

This encodes the ADR-0070 authority split precisely:

- the **workflow author** (pack) declares *what* — which dataflow, which
  stages, workflow-scoped config;
- the **platform team** (environment) declares *where and how* — which
  engine, which machine/cluster, resource and budget limits, governance
  presets.

**Scaling becomes a deploy decision**: one box → Ray on KubeRay → fleet by
editing an environment binding. Zero template or pack changes — the property
that makes "impossible to scale" structurally solved rather than patched.

### D5 — Deploy configuration per engine.

Packaging follows the proven `operator-training[local]` split — one wheel,
two install profiles:

```
operator-dataflow/                    # ONE operator-pack wheel
  operators/run.py                    #   dataflow/run  (no data deps)
  engine/                             #   cairn-dataflow-run console script
  stages/                             #   dedup.exact · decontam · pii.redact · …
  pyproject:
    base           → the operator only          (CONTROL PLANE installs this)
    [engine] extra → polars, duckdb, pyarrow,
                     presidio + model wheels    (JOB MACHINE installs this)
```

Deploy shapes per engine:

- **builtin / SSH box (rung 1)**: box installs `operator-dataflow[engine]`
  (+ pinned spaCy model wheels — models are not PyPI deps; caught live);
  a `compute.ssh` connection names the box; the control plane image carries
  **none** of the data-plane deps. This retires the `[fast]`/`[pii]`
  hermetic-fallback gymnastics in the engine image.
- **builtin / k8s Job (rung 2)**: a `cairn-dataflow` image (engine extra
  baked) launched as one Job per dataflow run; same spec via env, same
  heartbeats.
- **ray (rung 3)**: KubeRay cluster; the stage wheel ships via Ray
  `runtime_env`; the adapter maps StageRefs to `map_batches` kernels.
- Budget/limits and engine credentials are environment config; job machines
  receive **no** registry or Postgres credentials (D3 rule).

The control plane physically cannot execute a dataflow — the deps are not
installed. Packaging **enforces** the boundary the architecture declares.

### Prior art the design borrows from (anti-bespoke anchors)

No single project is copied wholesale; each layer steals from the project that
got that layer right — and inherits its named mistakes as guardrails:

| Layer | Borrowed from | What | Their mistake we avoid |
|---|---|---|---|
| Engine protocol | **Flyte Agents** + **Dagster Pipes** | tiny async create/get/delete interface for external engines; structured events back | Flyte's heavy type-transformer system — we stay manifests + scalars |
| One spec, many engines | **Apache Beam runner model** | portable spec, N runners; `DirectRunner` = our builtin (a first-class local runner, not a toy); `ValidatesRunner` = our conformance suite | Beam's lowest-common-denominator API — hence capability discovery |
| Dataflows in packs + adapters | **dbt** | declarative YAML in packages, per-backend adapters, manifest artifacts | config resolvable from five places — ADR-0070 layering already exists, add nothing |
| Content-addressed stage caching | **DVC** | `dvc.yaml` deps+outs+command hashing; repro skips unchanged stages | git-coupling — the registry, not the repo, is the source of truth |
| Artifact registry | **Metaflow datastore** + **OpenLineage** | content-addressed artifact store; adopt OpenLineage's job/run/dataset+facets vocabulary for lineage events (legible to Marquez/DataHub for free) | building a full catalog product — registry stays tables + API |

Fitting irony: Google Cloud Dataflow — the naming precedent we chose — *is*
Beam's hosted runner. The name commits us to the right architecture:
dataflow = portable spec, engines = runners, builtin = our DirectRunner.

---

## 4. Architecture (generic)

```
┌─ CAIRN CONTROL PLANE (unchanged product) ───────────────────────────┐
│   console · templates (graph) · gates · HITL · audit · policy ·     │
│   runs · provenance · bundle pipelines (ADR-0069, untouched)        │
│      one graph node = trigger + governance boundary                 │
└───────────────┬───────────────────────────▲─────────────────────────┘
                │ DataflowSpec              │ artifacts + stats (scalars)
                ▼                           │
        dataflow/run operator  (D1 — the only door; remote_job protocol)
                │ submit                    ▲ heartbeats · metrics · result
                ▼                           │
        DataflowEngine protocol  (D3 — core, engine-agnostic)
                │
   ┌────────────┼──────────────┬─────────────────┐
   ▼            ▼              ▼                 ▼
 builtin      ray on        dagster           llm-batch /
 (one box /   kuberay       (pipes            analytics
  k8s job)                   adapter)         (later)
   │  execution rungs — swapped by ENVIRONMENT BACKEND config (D4/D5)
   ▼
┌─ ARTIFACT REGISTRY (core substrate) ────────────────────────────────┐
│  content-addressed versions · lineage DAG · tags (HITL promotion) · │
│  write-time stats · fingerprints        postgres + parquet object   │
└─────────────────────────────────────────────────────────────────────┘
```

Execution flow of one governed dataflow:

1. Template node reaches `dataflow/run`; compile-time risk → the approval
   gate fires; a human approves the submission (one approval per dataflow).
2. The operator resolves the environment's engine backend, validates the
   spec, submits. State carries only the handle.
3. The engine executes — sharded, resumable, entirely its own business —
   streaming progress metrics up the existing job channel.
4. Mid-run gates (budget/quality) may cancel via the control loop.
5. On success the engine returns artifact descriptors + stats; the control
   plane registers artifacts (lineage: `input → job(code, config) → output`),
   stamps `parent_run` (#989).
6. The next gate judges the returned **stats**; HITL may promote the output
   artifact's **tag**; the report node narrates. The graph never saw a row.

---

## 5. Stages: operators are factored, not killed

The operator bodies are reused — `run_fix_pipeline` already chains
`DedupApplyOp.run() → DecontamApplyOp.run() → AnonymizeOp.run()` verbatim on a
job box. What changes is the **contract level** of invocation:

```
kernel libraries      _pii.anonymize_many · canonical_hash · minhash · iter_rows
      ▲                        ▲
   operator                  stage
   control-plane wrapper     data-plane wrapper
   (path in/out, ctx,        (ENGINE owns the loop; per-shard/per-batch
    self-driving loop,        kernel; manifest + stats out; no engine
    graph node)               ctx, no credentials)
```

- **v1**: a stage is a thin adapter around `operator.run()` — whole-dataset,
  single box, zero rewrite (this is exactly today's proven code, made
  spec-driven).
- **v2, per stage, only where scale demands**: expose the kernel as the map
  function (`pii.redact` → `anonymize_many` over batches; `dedup.exact` →
  hash-map + one reduce) so a distributed engine can own the loop. Inversion
  of control — who drives the iteration — is the entire difference, and the
  reason `Shardable` (#962) could never get there from inside `run()`.
- Stages declare **risk classes** like operators do; the compiler aggregates
  a dataflow's stage risks into the `dataflow/run` node's gate requirement
  (needs first-class `dataflows/`, hence the D2 phasing).
- Control-plane operators (`gate`, `provenance`, `sample`, `synthesize`,
  report assemblers) **stay operators forever** — they belong in the graph.

What actually dies: the **template bindings** of heavy data operators as
graph nodes, and the `fix`/`fix-remote`/`fix-batch` trio (replaced by one
`run-clean` binding). Code and tests migrate.

---

## 6. What this unlocks

1. **Scale as a deploy decision** — engine rung swapped in environment
   config; templates and packs untouched.
2. **LLM batch workloads** — 1M-row synthesis/judging/embedding as an engine
   draining provider batch APIs under a budget gate; currently unbuildable
   (one LLM call per graph node).
3. **End-to-end lineage** — model → split → cleaned → raw as one queryable
   artifact chain: the compliance product of a governed control plane.
4. **Incremental economics** — content-addressed caching + fingerprint
   anti-join: batch N pays only for its own new rows; unchanged work is a
   lookup.
5. **Mid-run governance as a platform primitive** — the learning-gate
   pattern generalized to any dataflow's metric stream.
6. **HITL on artifact-tag promotion** — approvals become "may this version
   go to `@prod`", uniform across datasets and models.
7. **Logs/traces on the same contract** — watch-triggered windowed dataflows
   feeding incident workflows.
8. **A console Artifacts surface** (versions, stats, lineage, promote) falls
   directly out of the registry. A **Dataflows** surface lists pack
   dataflows — cleanly distinct from the existing Pipelines (run chains)
   view.
9. **An extension market shape** — engines and stage packs are installable
   units, like operator packs today; `operator-dataflow` can later leave the
   monorepo as a true extension.

---

## 7. Migration plan

- **Phase 0** — core contract + artifact registry + manifest helpers **+ the
  engine conformance suite** (`tests/dataflow_conformance/`, parameterized
  over engines — the protocol's executable definition).
  (Everything hangs off these; no behavior change.)
- **Phase 1** — `operator-dataflow` wheel: `dataflow/run` + builtin engine
  (generalize `fix_entry`/`run_fix_pipeline`) + four stages
  (`ingest.normalize`, `dedup.exact`, `pii.redact`, `export.parquet`) +
  backend binding + `run-clean` operation (stages inline). The fix trio is
  replaced; live on the existing SSH box. Cross-batch fingerprints publish
  control-side from a returned hash artifact.
- **Phase 2** — `profile` stages + `check` cutover at scale; `dataflows/`
  first-class in packs (lock/validate/doctor/console); stage risk classes →
  compiler-forced gates; console Artifacts + Dataflows views; tag-promotion
  HITL.
- **Phase 3** — adapters in decided order: **Ray-on-KubeRay first** (the
  capability adapter — the only engine adding data-plane execution power),
  then **Prefect** (first orchestrator conformer — cheapest adapter + CI
  self-host), then **Dagster** demand-driven (trigger: a design partner who
  runs Dagster; its bridge design is already specified, so a fast follow).
  Plus: llm-batch engine; mid-run budget/quality gates; watch-triggered
  analytics dataflows. Standing guardrail for orchestrator adapters (R12):
  their schedules/sensors/automations are never used for cairn dataflows —
  cairn triggers, they only execute.

Each phase is independently shippable and independently falsifiable by a live
run — the ADR-0080 discipline.

---

## 8. Rejected designs

- **R1 — Stages as graph nodes (status quo).** Falsified by the epic itself:
  five shipped workarounds and two live outages are the evidence record (§1).
- **R2 — Embed Dagster as the data orchestrator.** Dagster is an
  orchestrator: webserver + daemon + its own Postgres + code-location
  servers + run launchers. Embedding it yields two control planes — two run
  stores, two schedulers, two UIs — with cairn's gates/HITL/audit *outside*
  the second one's view. Dagster's own external-execution protocol (Pipes)
  is the admission that orchestration and execution separate; cairn already
  has its Pipes (the remote-job spine). A Pipes *adapter* remains possible
  behind D3.
- **R3 — Build the engine into cairn core.** The core wheel would carry
  polars/duckdb/presidio, and the temptation to call the engine in-process
  returns — the exact disease. Packaging must enforce the boundary (D5).
- **R4 — Bypass the operator layer** (server→engine endpoint, or a special
  template node type). Loses compile-time risk gating, HITL, audit, policy,
  mock — every governance property attaches at the operator boundary. One
  door (D1) or the governance story fractures.
- **R5 — Operators as distributed stages without factoring.** An operator's
  `run()` owns its own loop; a distributed engine must own the loop and call
  kernels. Wrapping an operator in a Ray task runs it as one opaque unit —
  no shard parallelism is inherited. Kernel exposure (§5), not wrapping,
  unlocks distribution.
- **R6 — Row data / defect lists through control state.** Re-affirmed from
  ADR-0080 (R5/D6 there): detection gates emit scalars; transforms re-derive
  by streaming; the proof a fix worked is the re-check on the new version,
  not index bookkeeping between nodes.
- **R7 — Per-workload bespoke remote operators.** The
  `fix`/`fix-remote`/`fix-batch` trio is the cautionary tale: without one
  generic boundary, every workload×dispatch combination breeds an operator.
  N workloads × M dispatch modes collapses to one operator × pluggable
  engines.
- **R8 — Name the new concept "pipeline".** Triple collision (bundle
  pipelines / `ctx.pipeline_id` / this) across packs, API, and the console
  Pipelines view — see §2. Renaming the shipped bundle-pipeline surface was
  also rejected (breaking churn for zero product gain).

---

## 9. Consequences

**Positive.** The control plane becomes physically incapable of running data
work; scaling is configuration; governance attaches to *named artifact
transitions*; the registry becomes the load-bearing product surface;
per-workload engines (LLM batch, analytics) become additive extensions rather
than architecture events.

**Negative / accepted costs.** A new core substrate (registry) to operate and
migrate; two invocation contracts (operator + stage) over the same kernels —
mitigated by v1 stages being operator adapters; first-class `dataflows/`
extends the pack contract (deferred until it pays, D2); the builtin engine is
one more deployable to version and ship (mitigated: one wheel, extras split,
same repo initially).

**Explicitly unchanged.** Templates, gates, HITL, audit, policy, packs-are-
data, the operator registry, the remote-job spine, bundle pipelines
(ADR-0069) and their console surface, run lineage.

---

## 10. Open questions

1. Registry write path for *cluster* engines (rung 3): control-plane
   registration from returned descriptors holds, but very large stage counts
   may want batched/streamed registration.
2. Manifest format versioning — adopt a minimal Iceberg-compatible subset now
   or a bespoke JSON manifest first? (Bespoke first; revisit at rung 3.)
3. Does `prep` (anonymize + split) migrate in Phase 2 or stay in-graph until
   the check cutover proves the profile dataflow? (Lean: migrate with check.)
4. Whether `operator-dataflow` leaves the monorepo once stable (own release
   cadence as a true extension) — deferred until Phase 3.
