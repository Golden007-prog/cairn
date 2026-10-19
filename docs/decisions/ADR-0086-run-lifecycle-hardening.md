# ADR-0086: Run-lifecycle hardening — self-sufficient durable execution without an external orchestrator

- **Status:** Proposed
- **Date:** 2026-08-06
- **Related:** ADR-0081 (dataflow engine protocol), ADR-0082 (kind: ml flows),
  ADR-0084 D1 (cairn is a control plane; runtimes are adapters), #1078
  (Metaflow option-B), #1239 (orphan reconciler), #1241 (gate-downstream
  advance bug), #1243 (Metaflow bridge divergence)

## Context

The 2026-08-06 multi-engine E2E (Prefect data leg + Metaflow ml leg + LangGraph
report leg on one local control plane) settled the #1078-adjacent question "how
much of an external orchestrator does cairn actually need?" with a measured
answer: **none of its services, one of its checklists.**

Facts established against the code and live runs:

- Prefect's subsystems measure ~46k LOC (runner 6.7k, orchestration 4k,
  services 2.9k, workers 4.6k, api 11.3k, client 16k). They solve problems of
  *Prefect's* architecture — client-proposed state arbitration, pull-based work
  queues, workers as infra provisioners. cairn inverted all three by design
  (ADR-0084 D1): the control plane owns state, engines are dumb interpreters,
  resume is adopt-by-handle. The subsystems do not transplant; their failure-mode
  inventory (`core_policy.py`, ~31 named transition rules) does.
- What cairn measurably lacks is small and specific. Each gap surfaced as a
  real incident, not a hypothetical:
  1. **No leg retry.** Engines report a `retriable` flag
     (`engines_ssh.py` poll) that no caller consumes. `RetryPolicy`
     (`cairn/core/runtime/retry.py`) exists but only serves template nodes
     under Temporal.
  2. **No zombie detection.** Prod audit found `running` runs whose processes
     were long dead (#1239, #977's stale columns).
  3. **No transition guards.** `status=` is written ad hoc at ~a dozen call
     sites; #1241 (paused→running ordering dropped every leg downstream of a
     HITL gate) and #1239's stale columns are both instances of the same class.
  4. **Missed schedules are lost.** The Tier-1 scheduler is in-process
     apscheduler; schedules persist in the store but fires missed while the
     server is down vanish silently — no catch-up, no "late" concept.
  5. **No per-target concurrency cap.** Lane concurrency (realtime/background)
     is global; nothing stops N train legs from being SSH-dispatched to one
     GPU box simultaneously.
  6. **Stage checkpoints are box-local.** The builtin runner's `.done` markers
     (stage-granular resume, the run_fix_pipeline contract) live in a `/tmp`
     workdir on the executing box. Box loss = resume impossible. This is the
     one genuine *durability* hole, and it is an artifact-store problem, not an
     engine problem — no orchestrator adoption fixes it (Metaflow's S3
     datastore can't help either while the bridge is one step, #1243).

## Decision

cairn's own data/ml pipelines run on the **builtin engine (local/SSH) with no
external orchestrator**. Prefect and Metaflow remain conformance-tested
*integration adapters* — selected per environment for customers who already
operate those systems — never load-bearing infrastructure for cairn itself.
Durability is completed in cairn's own tier by six slices:

### H1 — Heartbeat + orphan reconciler (#1239)

Every in-flight leg's poll loop stamps `last_heartbeat_at` on the run record
(piggybacked on the existing `_poll_until_terminal` tick — no new loop on the
happy path). A reconciler sweep (apscheduler background lane, ~60s) terminalizes
any `running` run whose heartbeat has expired **and** whose engine handle no
longer resolves to live work: status → `failed`, error names the orphan cause,
terminal event emitted through the same bus path a normal completion uses (so
pipelines advance/fail rather than hanging). Restart adoption re-verifies
handles before re-mapping them. The TTL pattern is Prefect's
(heartbeat → CRASHED); the implementation is ~200 LOC against cairn's stores.

### H2 — Leg-level retry (consume `retriable`)

`execute_leg` wraps submit+poll in a bounded retry driven by the engine's
`retriable` flag and the *existing* `RetryPolicy` shape (max attempts, backoff,
max interval). Governance invariant: **every attempt is visible** — each retry
re-reserves budget, emits a `leg_retrying` event with attempt number and cause,
and the run record accumulates an attempts list. Non-retriable failures and
budget refusals never retry. Defaults conservative (2 attempts, data legs only
unless the flow def opts in) — a failed train leg must not silently re-spend
GPU dollars; `retry:` in the flow def or leg `with:` overrides.

### H3 — State-transition guard

One function — `transition(run, to, *, expect)` — becomes the only way core
mutates run/pipeline status. It enforces a small explicit table (pending →
running → terminal; awaiting_* ↔ paused/running; terminal states immutable)
and refuses-and-logs illegal transitions instead of writing them. The ~12
existing `status=` call sites are swept onto it. Prefect's rule names
(`PreventPendingTransitions`, `HandleFlowTerminalStateTransitions`,
`PreventDuplicateTransitions`, `EnforceCancellingToCancelledTransition`) become
the regression-test checklist. #1241 and the #1239 stale-column shape both
become mechanically impossible rather than review-caught.

### H4 — Schedule catch-up

The schedule store records `last_fired_at` per schedule. On scheduler start,
each schedule whose cron would have fired during downtime is handled per a
`misfire` policy: `skip` (default), `fire_once` (coalesce all missed fires into
one), or `fire_all`. The decision is audited either way — a governance plane
must be able to answer "why did the nightly readiness run not happen." This
closes the single-instance apscheduler gap without a second scheduler; the
Temporal runtime keeps its own timers (unchanged).

### H5 — Per-target concurrency cap

`max_in_flight` on a compute connection (default unlimited, honest zero-change
default). Enforced at leg submit against a count of live legs on that target;
excess legs wait in `queued` rather than being refused. Protects a single GPU
box from concurrent train legs and the SSH fd budget (#1180 bug 11's cousin).
This is admission-adjacent and belongs to the control plane, not to any engine.

### H6 (phase 2, own design pass) — Durable stage handoff

Stage outputs and `.done` markers replicate through the existing artifact store
(content-addressed, parents already modeled) so a leg can resume **on a
different box** from the last completed stage. Sketch: the runner already emits
an `ArtifactDescriptor` per stage; H6 makes the descriptor's URI durable
(S3/artifact-store backed) and teaches `execute_leg`'s resubmit path to seed a
fresh workdir from descriptors. This is deliberately NOT in the H1–H5 slice:
it changes the engine file contract and deserves its own ADR section with the
artifact-store owners. Until H6, box-loss recovery for a mid-flow leg is
"re-run the leg" (H2), with stage-level resume only on the same box.

## Consequences

- **After H1–H5:** a data/ml pipeline on the builtin engine survives control-
  plane restart (adopt-by-handle, already proven live), worker-process death
  (H1 terminalizes, H2 retries), transient infra failures (H2), server downtime
  across a cron boundary (H4), and noisy-neighbor submission (H5) — with every
  recovery action admitted, budgeted, and audited. That is the durability bar
  Prefect's server would have provided, at ~600 LOC instead of a second
  database, an Alembic migration surface, and a redundant scheduler/UI.
- Prefect adapter: unchanged, optional, R12 still bans its scheduler for cairn
  flows. Its Postgres — when a customer environment runs one — stays its own
  database, never cairn's (verified: cairn never reads Prefect state; sharing
  buys nothing and risks generic-name collisions and third-party DDL in the
  governance DB).
- Metaflow adapter: #1243 (bridge → `run_dataflow`, net deletion) is a
  precondition for calling it conformant; its S3 datastore becomes interesting
  only multi-node, and H6 may make it redundant.
- The conformance suite gains the H-slice invariants (heartbeat stamping,
  retriable propagation) so future adapters can't silently drop them
  (the #1243 lesson: vacuous assertions pass empty lists).

## Alternatives rejected

- **Adopt Prefect server + services as cairn's execution tier.** Second
  database + migrations in the governance plane, redundant scheduler/UI/RBAC,
  and none of the six gaps above actually closed by it (zombie detection helps
  only Prefect-executed legs; cairn's builtin/SSH legs — the default — would
  gain nothing).
- **Port Prefect subsystems into cairn.** ~46k LOC written against Prefect's
  ORM/events/settings; the transplantable fraction approaches zero. Apache-2.0
  permits it; engineering reality doesn't reward it.
- **Route all legs through Temporal for durability.** Temporal is already the
  heavy-durability option and stays available, but making it mandatory
  contradicts ADR-0084 D1 ("a pure data/ml pipeline loads neither LangGraph
  nor Temporal") and adds operational weight the builtin path doesn't need.

## Execution

| Slice | Issue | Size | Order |
|---|---|---|---|
| H1 reconciler | #1239 (rescoped to this ADR) | ~200 LOC | 1 |
| H2 leg retry | new | ~60 LOC + tests | 2 |
| H3 transition guard | new | ~150 LOC + sweep | 3 (before H1 lands its writes ideally; acceptable after) |
| H4 schedule catch-up | new | ~100 LOC | 4 |
| H5 per-target cap | new | ~80 LOC | 5 |
| H6 durable handoff | new (phase 2) | own design pass | after H1–H5 |

H1+H3 are the incident-driven core; H2 rides the same touched code; H4/H5 are
independent and parallelizable.
