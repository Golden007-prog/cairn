# ADR-0082 — Training subflow engines: Metaflow (and siblings) behind the train door

Status: **ACCEPTED for P0–P1** (2026-07-22 — kind named `ml`, prose "ML
flow"; epic #1071, issues #1072–#1081). §7 open questions must be resolved
before building P2/P3.

Relates to / builds on: **ADR-0081** (dataflow extension — the layering rule
this ADR applies to *training*: protocols in core, WHAT in packs, CODE in
operator packs, WHERE in environments), **ADR-0080** (dataset-as-reference:
the immutable snapshot URI + hash a training flow consumes), ADR-0034
(operator-chokepoint approval gates), ADR-0046/#302 (profile backends), #565
(remote-job queue routing), #1050 (`resolve_engine` seam + handle-stashed run
identity), #1051 (first-class pack flow defs), #1052 (gated artifact
promotion — the reason registration stays OUT of training flows).

Research basis: docs.metaflow.org ([Runner](https://docs.metaflow.org/metaflow/managing-flows/runner),
[Deployer](https://docs.metaflow.org/metaflow/managing-flows/deployer),
[Argo](https://docs.metaflow.org/production/scheduling-metaflow-flows/scheduling-with-argo-workflows),
[Step Functions](https://docs.metaflow.org/production/scheduling-metaflow-flows/scheduling-with-aws-step-functions),
[Client](https://docs.metaflow.org/metaflow/client),
[Infrastructure](https://docs.metaflow.org/getting-started/infrastructure))
+ the full brief in `docs/ideas/metaflow-training-subflow.md`.

---

## 1. Context

cairn's training node is one opaque remote job: a `train/*` provider
(`runpod`/`vast`/`modal`/`kaggle`/`local`/SSH) launches `train_entry` on a
machine; cairn sees `running → succeeded/failed` plus a metrics stream.
Failure granularity is the whole job (an eval crash after a 3-hour fine-tune
re-spends the GPU), one machine profile holds for the whole duration, and the
console cannot see *where inside* the job a run is.

Metaflow's flow model fixes exactly the inside of that box: per-step
`@retry`/`@resources`, per-step artifact snapshotting with `resume`, cheap-CPU
packaging after GPU training, and per-run code+data reproducibility —
scheduled by Argo Workflows (k8s) or AWS Step Functions (Batch), which also
replaces hand-managed GPU fleet logic for this path.

The temptation to reject: making Metaflow (or Prefect) a *peer orchestrator*
that owns gates or registration. Gates, approvals, promotion, and audit are
cairn's product; engines provide infrastructure (ADR-0081's hard rule).

## 2. Decision (proposed — needs discussion)

**D1 — Metaflow is a training SUBFLOW under the existing remote-job
protocol, not an orchestrator and not a DataflowEngine.** `train/metaflow`
implements submit → poll → cancel exactly like `train/runpod`:
`DeployedFlow.trigger(**params)` → pathspec in the `JobHandle` →
`Run(pathspec)` polls (stateless re-attach from any process — docs-verified)
→ `TriggeredRun.terminate()` cancels. `executors=["remote_job"]` routes it to
the remote queue + durable control loop; HITL/resume/restart-adopt inherited.

**D2 — Prefect and Metaflow never talk to each other.** Both are leaf
executors under cairn nodes. The handoff between the data leg and the training
leg is a **registered artifact** (immutable dataset URI + hash, ADR-0080), not
a call. (This deliberately rejects the "Prefect launches Metaflow" pattern.)

**D3 — Layering (the ADR-0081 sentence, applied to training):**

| Layer | Owns |
| --- | --- |
| core | remote-job protocol + control loop (**exists — no new protocol**); `ml_engine`/`ml_target` backend fields mirroring `dataflow_engine`/`dataflow_target` (engine-neutral — never `metaflow_*`); doctor check; artifact registry lineage |
| packs (DATA) | `finetune.md` **byte-unchanged**; `run-train.yaml` binding maps `$input`/TrainSpec → flow Parameters; schemas. Never names a vendor, deployment, or cluster |
| operator packs (CODE) | `train/metaflow` operator (~150 lines, runpod shape) + the FlowSpec whose steps are thin adapters over the PROVEN `train_entry`/eval bodies (kernel rule, ADR-0081 §5) + `[metaflow]` extra + fake-seam tests |
| environments | deployment identifier · namespace · metadata-service URL · MLflow URI · creds via connection catalog. Swap Argo ↔ Step Functions with zero pack/operator change |
| CI + infra | `argo-workflows create` on merge (the deployment is a BUILD ARTIFACT; its identifier lands in environment config); metadata service + S3-class datastore + k8s/Argo or Batch/SFN |

**D4 — Governance placement is not negotiable:** cost gate before submit,
learning gate polls **MLflow** mid-run (Metaflow's Client API exposes only
*completed-step* artifacts — a 3-hour train step is a black box to it;
`train_entry` already logs progressively via `report_to`), model gate after,
promotion via the GATED run (#1052). The flow **packages** a candidate URI; it
never registers or promotes. `retries=0` on cairn's side — `@retry` inside the
flow owns step retries; `cairn_run_id` is the idempotency key (#551 lesson).

**D5 — Runner is dev-only, never production.** It orchestrates in-process
(docs: the launching process must stay alive) — a worker restart orphans the
GPU run, the exact #601/#565 failure class. Production = Deployer-created
deployments: Argo first (`DeployedFlow.from_deployment` is argo-only today);
Step Functions viable with a held `Deployer` (note: Metaflow 2.18
conditional/recursive steps unsupported there — the flow stays a straight
line: `start → train → evaluate → package → end`).

**D6 — Identity:** one key, `cairn_run_id` (run_id-is-the-unit). Metaflow run
tagged `cairn:{run_id}`; MLflow parent/child runs tagged with run_id +
pathspec + dataset_hash + image_digest; registry `from_run` = run_id.

**D7 — ML flows are DECLARATIVE core specs, not hand-written engine flows
(user direction, 2026-07-21 — supersedes this ADR's earlier draft posture).**

*Naming + taxonomy (user-decided, 2026-07-22):* ONE pack kind — `flows/` —
with a `kind:` discriminator, not parallel directories. **A flow's kind is
the ARTIFACT it exists to produce** (mirrors the registry's artifact kinds):
`kind: data` → **dataflow** (product = dataset artifact; rows in/out; shared
working set; uniform CPU; data-governance risks) · `kind: ml` →
**ML flow** (a flow of ML STEPS; product = model artifact; dataset REFERENCE
in; heterogeneous per-step resources; checkpoint/resume; cost + quality
gates; metrics stream to MLflow mid-run). Engines map to kinds in
ENVIRONMENT config, many-to-many
— Metaflow is a generic engine, the KIND is what's specific. Edge cases the
rule settles: batch inference / embeddings / judge sweeps USE a model but
PRODUCE data → `kind: data` with model-backed kernels (the planned llm-batch
stages). Rejected names: `mlflow` (fatal collision with MLflow the product —
`kind: ml` inside `flows/` never forms that token), `trainflow` (too narrow
— excludes quantize/distill/merge/eval), `modelflow` (the kind is a flow of
ML steps, not a flow "of models"), `pipeline` (reserved by ADR-0069
bundles), `computeflow`/`gpuflow` (vague / implementation detail).
Migration: `dataflows/` (#1051) becomes the first `flows/ kind: data`
citizen with a back-compat alias.
The scaling argument: hand-written vendor flows cost N pipelines × M engines;
the declarative model costs N + M + K (N YAML flow defs in packs · M one-time
engine bridges · K kernels in operator packs). Therefore:

- **Spec in core:** an ML flow is the SAME contract family as a dataflow —
  `StageRef` already carries per-stage `resources` (`StageResources`, unused
  by data kernels). Pack YAML (`flows/*.yaml` with `kind: ml` — the #1051
  first-class kind, generalized) declares stages + configs + per-step
  resources; the compiler's
  structural gating (fail-closed risk → compile-required approval) applies
  unchanged because it reads the spec, not the engine.
- **One generic bridge per engine, written once — and ENGINE-NEUTRAL by
  contract (user directive, 2026-07-22: "don't build too tight to Metaflow
  — it can be any other ML pipeline, same as Prefect").** The bridge
  contract is "interpret the spec's ORDERED STAGE LIST (arbitrary length)":
  resolve each stage's KERNEL from the entry-point registry, honor per-step
  resources, report stage events. Prefect/Flyte/Dagster/Kubeflow iterate
  stages dynamically; Metaflow's static-DAG constraint means ITS bridge is
  a generic linear interpreter flow — an implementation detail private to
  the Metaflow bridge, never a spec constraint. Core carries engine-neutral
  backend fields only (`ml_engine`/`ml_target`, mirroring
  `dataflow_engine`/`dataflow_target` — no `metaflow_*` anywhere in core);
  the conformance suite is the real contract, and any engine that passes it
  is a peer (the PrefectEngine-second-conformer precedent). Metaflow is the
  FIRST P2 bridge, not a privileged one. The door is likewise
  kind-symmetric: **`ml/run`** (not `train/run` — same too-narrow reasoning
  as the rejected `trainflow`), mirroring `dataflow/run`.
- **Operators are the workbone:** kernels (`train.qlora` wrapping
  `train_entry`, `eval.checkpoints`, `package.adapter`) live in operator
  packs via the registry. Custom training logic = a custom KERNEL, never a
  hand-written engine-native flow.
- **The door stays an operator** (thin, generic, ~100 lines): that is the
  governance chokepoint — requires_approval, remote-queue routing, budget,
  audit all attach there. Flow CONTENT scales via spec+kernels; the door
  never grows.

**D8 — ONE-PIPELINE INVARIANTS (user requirement, 2026-07-22): flows must
not fracture the single-run experience.** Whatever engine executes a stage,
the pipeline remains ONE governed entity with the durability it has today:

1. **run_id is the unit** (D6): every engine run tagged `cairn:{run_id}`;
   handle stashes it (#1050); MLflow + artifacts key on it. One key.
2. **Durability layered, not moved:** Temporal owns the graph node; the flow
   is a remote job under the existing durable control loop (#565 routing,
   #601/#551 adopt-not-resubmit, #603 cancel). Engine step-durability
   (Metaflow `resume`, Prefect retries) nests INSIDE the node.
3. **HITL is one queue:** run-scoped gates keep interrupt →
   `awaiting_approval` → resume; engine-native in-flow pause
   (`supports_pause`, pending) MUST surface through the SAME approval
   surface. The user sees one approval model.
4. **watch (#992) unchanged** — triggers propose workflows; flows are
   invisible to the trigger plane.
5. **Artifacts + lineage are the only inter-plane currency:** every flow
   output registered `from_run=run_id`; P0 pins
   dataset_hash + image_digest + TrainSpec so dataset→model is walkable.
6. **Stage events are a protocol expectation, not a courtesy:** every
   engine bridge MUST translate stage transitions + stats into cairn run
   events (the `hoist_scan_findings` pattern, generalized). Run timeline,
   RCA correlation, auto-eval/insights (#484-486), and `ask` then see
   engine work with zero engine-specific UI. Conformance flag:
   `emits_stage_events`.
7. **Combined graph view:** because flows are DECLARATIVE specs, the
   console renders a flow node expanded into its stages FROM THE PACK YAML
   (no engine introspection; identical across builtin/Prefect/Metaflow),
   with live stage status overlaid from hoisted events. Hand-written
   engine flows could never be drawn this way — a further argument for D7.
8. **Bundles compose unchanged:** multi-leg pipelines
   (check→clean→promote→train→benchmark) remain ADR-0069 PipelineBridge
   chains — artifacts as the joints, series grouping/budget on top.

New work these invariants add to the rollout: (a) generalize stage-event
hoisting into the bridge contract + conformance (P1); (b) console
flow-node expansion in the graph view from the spec + status overlay (P1,
console); (c) route in-flow pause into the existing approval queue (the
pending HITL item, P2 with the Prefect/Metaflow bridges).

## 3. What this buys (the case FOR)

Resume-from-failed-step instead of re-spending the GPU · per-step retry ·
per-step hardware (packaging on cheap CPU, not the rented A100) · per-run
code+data snapshots addressable forever (`CairnFineTuneFlow/8472`) · fleet
management delegated to k8s/Batch · step-level progress + later `foreach`
checkpoint fan-out (Argo). Felt on every long, expensive, or failed run.

## 4. What it costs (the case AGAINST)

A fourth system to operate: metadata service + DB, S3-class datastore,
k8s/Argo or Batch — for a capability whose reproducibility half is ~80%
achievable today by pinning `dataset_hash + image_digest + TrainSpec` into
lineage (do that FIRST regardless). Benefit is thin while training runs are
short/cheap; real once mid-run failures hurt.

## 5. Rollout (if accepted)

Epic: **#1071**.

- **P0 (#1072 — free, do now):** pin dataset_hash + image_digest + TrainSpec
  into run lineage — the reproducibility floor, no new infra.
- **P1 (D7 shape; #1073 #1074 #1075 #1076 #1077):** `kind: ml` flow spec in
  core (reusing StageRef/StageResources) + `flows/*.yaml` pack kind + ML
  kernels (`train.qlora` wrapping train_entry, `eval.checkpoints`,
  `package.adapter`) + the thin `ml/run` door; proven on the BUILTIN
  engine first (sequential, one box) — no Metaflow infra needed yet. D8
  invariants land here too: stage-event bridge contract (#1076) + console
  combined graph (#1077).
- **P2 (#1078 #1079):** the Metaflow BRIDGE (one generic fixed-skeleton flow
  honoring per-step resources) + backend fields, live against local-mode
  Metaflow; engine-native in-flow pause routed into the ONE approval queue.
- **P3 (#1080):** Argo deployment via CI; `from_deployment` trigger;
  acceptance at scale. Convergence of the legacy seven `train/*` doors
  follows (§7 Q1 = #1081).

## 6. Rejected

- **Prefect owns quality gate / model registration** (the original external
  sketch): moves the governed decision out of the auditable plane; contradicts
  ADR-0081 and #1052.
- **Metaflow as a peer/top-level orchestrator**, or **as a DataflowEngine
  adapter** (re-rejected from ADR-0081 research: client-side framework, no
  server API; its scheduler IS Argo).
- **Runner in production** (D5) · **`train → evaluate → package` represented
  in two systems** (single ownership; cairn sees ONE train node).
- **Hand-written per-pipeline engine flows** (this ADR's own first draft):
  costs N×M and puts flow logic in Python where packs can't declare it —
  replaced by D7 (declarative spec + one bridge per engine + kernels). The
  earlier "rejected: compiling pack YAML into FlowSpec" is WITHDRAWN — the
  bridge does not code-generate a FlowSpec per pipeline; it is ONE generic
  interpreter flow per engine, which avoids the codegen trap while keeping
  the spec declarative.

## 7. OPEN QUESTIONS — discuss before building past P1

1. **One door vs eight.** Dataflow converged on ONE `dataflow/run` (R7);
   training has seven provider operators. Converge them into the `ml/run` +
   `ml_engine` backend door (the `resolve_engine` shape, no new core protocol —
   TrainSpec is already the shared input)? Consistent story vs a churny
   refactor of proven operators. **Recommend: yes, but as its own ADR/phase.**
2. **Is the infra bill worth it now?** Solo-founder ops budget vs metadata
   service + datastore + cluster. P0+P1 (local-mode) defers this; P2 commits.
3. **Namespace strategy.** Client API reads are namespace-filtered — pin
   `namespace(None)` in the operator, or a dedicated service namespace?
4. **Where does `evaluate` end and cairn's `run-benchmark` begin?** Flow does
   checkpoint-selection eval (needs the GPU + checkpoints); cairn does the
   governed benchmark/eval-delta on the packaged candidate. Confirm the seam.
5. **Does `watch` (#992) proposing `training/finetune` need anything new?**
   Believed no (proposed_workflow is an input) — verify at P1.
