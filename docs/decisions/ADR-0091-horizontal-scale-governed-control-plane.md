# ADR-0091 — Horizontal scale for the governed control plane

Status: **PROPOSED** (2026-08-21 — epic #1528, slices #1529–#1533).

Relates to / builds on: **ADR-0084** (control plane: ONE flow concept, runtimes
and engines as adapters — the posture that makes "scale execution out, keep the
gate central" correct), **ADR-0089** (Metaflow as governed compute substrate —
the data/ML execution tier this ADR treats as already-scalable), **ADR-0088**
(durable stage handoff — the checkpoint/resume this must not regress),
**ADR-0080 P4 / #1032** (the data-plane job-machine fleet — the *execution*-tier
counterpart to this ADR's *control-plane* focus). Consumes the read-path and
correctness fixes from the 2026-08-21 architecture audit (#1505 authZ, #1508
list projection, #1509 event-seq race, #1510 retention).

Research basis: verified against the tree at `4b2cd8e3` (dispatch, runtime,
budget, bus modules quoted below) + the durable-execution reference
architectures (Temporal frontend/history/matching split; Ray GCS-in-Redis;
Prefect work-pool limits; Metaflow `@kubernetes`/`@batch` + S3 datastore). The
`verify-upstream` gate was run for this area: Temporal is the durable engine
already adopted (`OBS_RUNTIME=temporal`), LangGraph provides agent state +
checkpointing, and the shared store is Postgres — so the work below is
**externalize existing in-process state onto infra we already run**, not build a
distributed scheduler.

---

## 1. Context — what scales today and what does not

Cairn's execution tiers already fan out of the control plane:

- **Data / ML flow legs** dispatch to Metaflow with `kind: local | kubernetes |
  batch` and an S3 datastore (`engines_metaflow.py:137-199`). Heavy compute
  already leaves the control-plane process; lineage is preserved through the
  Metaflow datastore + cairn's artifact registry (`producer_run_id`).
- **Agentic flow legs** can run under `OBS_RUNTIME=temporal`
  (`settings.py:187`, `profile_backends.py:90`): the same LangGraph StateGraph
  executes across Temporal workers — pure nodes inline in the workflow, IO/LLM
  nodes reified as activities (`run_node`) with retries/timeouts
  (`runtime/base.py:7-27`, `temporal/activities.py`), HITL pausing *inside* the
  durable workflow (`pilot.py:293`), routed across three task queues — main,
  a pull-based remote queue, and a dataset queue (`temporal/worker.py:197-289`).
- **GPU / remote legs** use the SSH engine + the pull-based remote task queue.

So "can the work scale" is already **yes** for every leg kind. What does **not**
scale is the **control-plane process itself**:

- `uvicorn.run(app, ...)` runs a **single worker** (`triggers/main.py:136`).
- Governance and concurrency state is **in-process, module-global**: the series
  budget hold `_held` (`budget_reservations.py:52`), the pipeline coordination
  maps `_locks` / `_target_free` / `_run_to_pipeline`
  (`pipeline_bridge.py:204-224`), the target-concurrency in-Python run scan, and
  the `_EventHub`.

**The trap:** you cannot scale by raising `workers=N`. Each process would hold a
separate `_held` dict and a separate concurrency counter, so two processes would
both admit past a budget or concurrency cap — a **silent governance failure**.
The single-writer property is currently load-bearing and undocumented.

The saving grace for durability/lineage: **governance is enforced at admission,
not in the execution tier.** `enforce_gate` is the shared chokepoint called
before an operator runs, from both the operator path (`operator/runtime.py:800`)
and the task-leg path (`pipeline_bridge.py:990`); run records and lineage live
in external Postgres; durability is Temporal. Fanning execution out therefore
cannot weaken governance/durability/lineage — **only multiplying the control
plane can**, and only through the in-process singletons above.

## 2. Decision

Adopt a two-tier scaling model and make the **control plane horizontally
scalable by externalizing coordination onto infrastructure already in the
stack (Postgres + Temporal)** — not by building a distributed scheduler.

### Tier 1 — Execution scale-out (available now; governance-safe)
Keep, document, and harden the existing fan-out: Metaflow→K8s/Batch for data/ML,
`OBS_RUNTIME=temporal` worker fleet for agentic, SSH+remote-queue for GPU. The
only missing piece is **proving the Temporal agentic path live** (today it is
scaffolded, not campaign-proven). This tier needs no architectural change.

### Tier 2 — Stateless control plane over durable backends (the build)
Make the API tier stateless (N replicas behind a load balancer) by moving the
three singleton concerns to shared/durable state:

1. **Budget reservation** → a shared Postgres table with atomic reserve/settle.
   The lifecycle already exists in `leg_executor` (reserve→settle/release); only
   the *store* changes from a module-global dict to a row with `SELECT ... FOR
   UPDATE` / conditional upsert, so the budget gate holds across replicas.
2. **Target-concurrency** → a store-side `count_by(status, compute_target)`
   query instead of the in-Python scan of ≤500 runs, so the cap is fleet-wide.
3. **Pipeline coordination** (`_locks`/`_target_free`/`_run_to_pipeline`) →
   externalized: Postgres advisory locks or a work-claim table, **or** a
   leader-elected/Temporal-owned pipeline owner so there is one writer per
   pipeline without a process affinity.
4. **Durable ownership** → promote the existing `pipeline_workflow.py` to be the
   pipeline owner under Temporal, so Temporal provides single-writer semantics,
   retries, durable timers, and cross-machine resume — the reference
   frontend(stateless)/history(sharded)/matching(queue) template — and the API
   process becomes a stateless submitter + reader.
5. **SSE / events** already work cross-process via the Postgres bus
   LISTEN/NOTIFY bridge; this is unblocked once the `append_event` sequence race
   (#1509) is fixed so concurrent writers don't drop events.

`enforce_gate` stays exactly where it is — called inside the workflow/dispatch
path before each activity — so it remains the single admission chokepoint with N
replicas. Governance, durability, and lineage are preserved by construction.

## 3. Options considered

- **Option B — single governed control node + execution fan-out (near-term,
  chosen as the immediate posture).** One control-plane process is the admission
  authority (gate + bookkeeping + SSE, no compute); all heavy work goes to
  Metaflow-K8s and Temporal workers. Everything stays intact for free; ceiling is
  one process's admission/SSE throughput and a single point of failure (softened
  because Temporal holds in-flight state, so a restart resumes). **Sufficient
  until run-rate is high; the honest default today.**
- **Option A — stateless API + Temporal-owned durability (medium-term, this
  ADR's target).** The Tier-2 build above. Reuses Temporal's proven horizontal
  scale; the work is externalizing 3–4 singletons, not inventing coordination.
- **Option C — Argo Workflows as the data/ML orchestrator (scoped, optional).**
  Point Metaflow at Argo for autoscaling DAG scheduling + backfills. Real, but
  **orthogonal to control-plane scale** and largely redundant with the K8s task
  path already wired; take it only for Argo's scheduler/backfill semantics.
- **Rejected — raise `uvicorn workers=N` as-is.** Silently breaks budget and
  concurrency governance (per-process singletons). Named here so it is never done
  as a "quick scale" without the Tier-2 externalization first.
- **Rejected — build a bespoke distributed scheduler / distributed lock service
  in cairn.** Temporal already is the scale substrate; duplicating it is the
  anti-pattern `verify-upstream` exists to catch.

## 4. Consequences

- **Positive:** a genuine horizontal-scale path that keeps the governance
  chokepoint central; no new infra (Postgres + Temporal already run); execution
  scale is already delivered, so the remaining work is bounded and mechanical.
- **Cost:** externalizing budget/concurrency adds a hot-path DB round-trip at
  admission (mitigated by the same connection pool already in use); Temporal
  becomes a hard dependency of the durable path (already true under
  `OBS_RUNTIME=temporal`).
- **Risk / must-not-regress:** the read-path and event-seq fixes (#1508/#1509)
  are prerequisites — a stateless fleet amplifies both; ADR-0088 durable
  checkpoint/resume must survive the pipeline-owner move.

## 5. Rollout

Sequenced as the epic's child slices: (1) prove agentic-on-Temporal live; (2)
externalize budget to Postgres; (3) store-side target-concurrency; (4)
externalize pipeline coordination / Temporal-owned pipeline; (5) stateless API
tier + deploy (multi-replica, LB, SSE cross-process). Data-plane execution fleet
stays tracked under #1032. Ship behind the existing `temporal` profile gate
(`deploy_check.py`) so a mis-set runtime fails loud, not silent.

---

## Amendment 1 (2026-08-24) — the coordination substrate is Postgres; Temporal is optional

**Owner decision.** Section 2's tier-2 list left "Postgres advisory locks
**or** a Temporal-owned pipeline owner" as an open fork. It is closed:

1. **Control-plane coordination is Postgres, always.** Locks, counts, budget
   and event routing — the already-required dependency, and the substrate
   the tier-2 slices had in fact already chosen (#1530 budget via
   `pg_advisory_xact_lock`, #1531 store-side counts, #1532p1 message-carried
   event routing, #1532p2 session-scoped advisory locks for the pipeline and
   admission sections).
2. **Temporal is never a prerequisite for scaling the control plane.** The
   owner's reason is the engine-neutral thesis applied to cairn itself:
   future data/ML engines (Ray, Spark, Dask) bring their own orchestration
   and fault tolerance, and a shop running them gets no marginal value from
   a mandatory Temporal cluster. Requiring one as the price of a second API
   replica is an adoption tax on exactly the deployments cairn targets.
3. **Temporal remains the supported — and only proven — durable fleet for
   agentic bodies.** Multi-worker agentic deployment = the Temporal worker
   fleet (live-proven 2026-08-23/24: two workers, one killed mid-pipeline at
   a HITL gate, one approval on the survivor ran the pipeline to `done`).
   `in_process` under N replicas is load-spreading with runs pinned to the
   accepting replica and no body durability — supported and stated, never
   silently upgraded.
4. **Data/ML flow handling under Temporal is unchanged.** The
   `PipelineWorkflow` submit-and-await of `flow:` legs stays as built;
   engines stay the execution backends either way.
5. **DBOS is experimental until live-proven.** It has hermetic tests and no
   deployment history; this repo has now twice paid for treating tested as
   proven (the registry installer, the resume path). It is not offered as a
   fleet or durability answer until a live session proves it.

The per-kind options matrix behind this is recorded on epic #1528
(2026-08-24 comment). Remaining engineering consequence: the per-process
singletons (cron scheduler, #976 reconciler) need leader election — a
session-scoped advisory lock on the same substrate — scoped into #1533.
