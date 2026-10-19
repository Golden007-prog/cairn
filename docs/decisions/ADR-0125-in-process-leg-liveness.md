# ADR-0125 — An in-process leg names its owner process; restart, cancel and the reconciler ask whether that process is alive

- **Status:** ACCEPTED. The owner chose "owner id + heartbeat", a one-off cleanup script and an
  idempotency key (2026-10-14, #2555). After review showed that periodic heartbeat writes clobber
  results and fall silent on a blocked loop, the owner approved the owner lock in D2 ("Approve owner
  lock", 2026-10-14). The review refinements below (SQLite file lock, lock loss, key space) stay
  inside that approved design.
- **Date:** 2026-10-14
- **Issue:** #2555 (follow-up to #2540, PR #2554). Builds on ADR-0086 H1 (the heartbeat
  reconciler), #1289 (restart risk rule), #1787 (finalizers) and #2540. Those records are not
  edited.

## Context

#2540 (PR #2554) made a restarted control plane recover in-process pipelines without silently
repeating a side effect. Liveness, though, is still judged per process, from the bridge's own task
table. That fails in three ways (#2555):

- **Cancel, then crash.** A leg still `running` when its process dies after a cancel stays
  `running`. Adopt lists only running and paused pipelines (`dispatch/pipeline_bridge.py:358`).
  The reconciler's heartbeat verdict skips runs with no heartbeat (`runtime/reconciler.py:118-120`),
  and in-process task and template legs never beat.
- **A second replica.** Adopt is not leader-gated: `pipeline_bridge.start` is called directly at
  `triggers/server.py:742`. A replica that boots next to a live one fails that replica's live
  task leg through #1289 (`pipeline_bridge.py:397-405`). The Helm chart rolls pods with
  `RollingUpdate` when persistence is off (`deploy/helm/cairn/templates/server-deployment.yaml:20-23`).
- **Cancel across replicas.** #2540 therefore narrowed cancel to `awaiting_approval` runs
  (`pipeline_bridge.py:2198-2217`).

What exists today:

- `Run.last_heartbeat_at` (`storage/protocols.py:196-201`), held in the run's JSON `data` blob.
- Only flow legs stamp it, through `FlowLegHooks.heartbeat` (`dispatch/leg_hooks.py:58`, `:143`),
  and only while polling. The minimum gap between beats is `_HEARTBEAT_MIN_S = 10.0`, and in
  practice beats land every 10 to 20 s (`dispatch/leg_executor.py:38-39`, `:493-496`). Task and
  template legs have no hooks object.
- The reconciler runs as a leader-elected loop on the in-process runtime too
  (`triggers/server.py:593-608`, `:736-738`). Its own docstring (`reconciler.py:16-18`, `:276`) says
  "only under the temporal runtime". That is a documentation/implementation conflict, and this ADR
  corrects the docstring.
- Fleet coordination already uses Postgres session advisory locks as liveness, with no clock
  arithmetic: leadership (`core/leadership.py:15-20`) and the per-pipeline critical sections.

**Why not a periodic heartbeat for task and template legs.**

- *It can clobber a result.* `PostgresRunStore.update` is a full-row read-modify-write without a
  lock (`storage/postgres/runs.py:179`, `:202-215`). A timer-driven beat that reads `running`
  before the leg writes `done` would write `running` back and drop the result. The flow beat is
  safe only because it runs in the same coroutine as the leg's writes.
- *It lies when the loop blocks.* #2540's root cause was an operator that blocked the event loop.
  A heartbeat goes silent with it, and the leg is declared dead while it is still running.

## Decision

**D1 — a leg names its owner process.** `Run` gains `owner: str = ""`. It is held in the JSON
`data` blob, like `last_heartbeat_at`, `trace_id` and `bundle_pipeline_id`
(`storage/protocols.py:216-229`), so there is no column and no migration.
- The value is `"<hostname>:<boot id>"`, where the boot id is random and generated once per
  process start.
- It is written when the Run is created for an in-process leg (task, template, or a flow leg's
  poll loop).
- It is rewritten, from a fresh read inside the per-pipeline critical section, whenever a process
  adopts, re-attaches or claims the leg.
- Durable runtimes (Temporal, DBOS) do not set it. Their own liveness, the reconciler's
  `status_fn` and `owner_alive_fn` (`reconciler.py:172-187`), keeps precedence.

**D2 — a process proves it is alive by holding a lock.** A new core module, `core/liveness.py`,
sits next to `core/leadership.py` and owns the boot id, the owner id and the owner lock. It is a
process-level singleton holding a pinned connection, named here for the owner's approval under
§5. Every process that executes or adopts in-process legs, the server and a `cairn pipeline run`
CLI process alike, holds an owner lock for its lifetime:

- *Postgres store.* A session advisory lock in the two-int4 key space,
  `pg_advisory_lock(<owner class id>, hashtext(owner))`. The two-int4 space is separate from the
  single-key locks (leadership, critical sections, budget reservations). It is already shared by
  schema init, `(hashtext('cairn:schema-init'), 0)` in `core/pg_schema_init.py`, and by run events,
  `(hashtext('cairn:run_events'), hashtext(run_id))` in `storage/postgres/runs.py`. So
  `<owner class id>` is a fixed constant that must differ from both those first keys, and a test
  asserts it. A lifetime owner lock can then never block schema setup or an event append.
  - It sits on a dedicated size-1 pool with a unique purpose (the #2543 lesson).
  - It runs with TCP keepalives on.
  - A re-acquire loop, like leadership's, checks it every poll interval. It acquires with a short
    `lock_timeout`, not a bare try, so a probe's momentary transaction lock cannot make the owner
    skip an interval.
  - If a fresh process cannot take its lock on the first attempt, it regenerates its boot id
    (nothing references it yet). Two live owner ids that collide in `hashtext` therefore never
    leave a process permanently locked out.
- *SQLite store.* An OS file lock on `<db dir>/.cairn-owners/<owner>.lock`, taken with `flock`
  and held for the process's life. SQLite is single-host, so a file lock is exact. Lock files
  whose non-blocking `flock` succeeds belong to dead owners and are deleted at start-up. `flock` is
  not reliable on NFS-backed volumes, a deployment assumption.

A process that does not hold its owner lock at start-up, or loses it, adopts nothing until it
re-acquires. Its live legs keep running. A leg it would start in that state stays `pending` (or
`queued`). Re-acquiring the lock runs one advance pass over the pipelines this process held back,
which submits those legs. No launch is refused, and no API returns a new error.

Deployment assumptions, also listed in Consequences:
- a direct or session-mode Postgres connection, because PgBouncer transaction pooling breaks
  session locks;
- `idle_session_timeout = 0`;
- no proxy that idles out a quiet connection.

**D3 — one liveness function.** `owner_verdict(run, probe)` lives in `runtime/reconciler.py`, next
to the TTL and the existing verdicts. The probe is injected, `lock_free(owner) -> Optional[bool]`,
the way `owner_alive_fn` is (`reconciler.py:48-55`), and is implemented in `core/liveness.py`.

The probe never runs on the owner's own pinned connection, because session locks are reentrant.
- On Postgres it runs `pg_try_advisory_xact_lock(<owner class id>, hashtext(owner))` in a short
  transaction on the store pool, so the probe's lock always auto-releases.
- On SQLite it tries a non-blocking `flock` on the owner's file.

The function applies to leg Runs whose status is `queued` or `running`. `awaiting_approval` and
durable-runtime-owned runs are out of scope. The first row that matches wins:

| # | Run | Verdict |
|---|---|---|
| 1 | `owner` is this process's owner id | ALIVE, whichever bridge or path holds it |
| 2 | the probe raises or times out | UNDETERMINED |
| 3 | `owner` set; the probe finds the lock held | ALIVE |
| 4 | `owner` set; the probe finds the lock free on two probes more than one re-acquire interval apart | DEAD |
| 5 | `owner` set; the lock was found free once | UNDETERMINED until the second probe |
| 6 | `owner` empty, written before this ADR | LEGACY: the existing #2540 rules and the reconciler's flow-leg heartbeat verdict apply |

UNDETERMINED never triggers a destructive action. Only the reconciler sees two probes. It holds the
first-free observation in memory per owner and resets it when leadership changes. Adopt and cancel
are one-shot passes and only ever see rows 1-3, 5 and 6, so in practice DEAD legs are acted on by
the reconciler's sweep through the handoff in D4.

There is no clock arithmetic and no hostname shortcut. The two-probe rule means a Postgres restart
or failover, which drops every owner lock at once, does not turn live legs DEAD: their processes
re-acquire within one interval. The reconciler probes once per distinct owner per sweep, not once
per run.

**D4 — what each caller does with the verdict.**

- *Adopt (restart), and the reconciler's handoff.* For a DEAD leg:
  - a running task leg goes to #1289 (existing);
  - a template leg, or a flow leg without a handle, is failed and parked `awaiting_advance`. This is
    new with #2555 item 1; before, such legs were only re-mapped;
  - a flow leg with a handle is re-attached;
  - a #1787 finalizer is reset for re-run.

  An ALIVE or UNDETERMINED leg owned by another process is only re-mapped and left alone; the
  reconciler decides later. A LEGACY leg keeps the #2540 behaviour.
- *Cancel.* Cancel settles child Runs at `awaiting_approval` to `cancelled` (as now). Being
  one-shot, it never sees DEAD (it needs two probes), so it leaves `queued`/`running` legs alone.
  The reconciler's sweep settles a DEAD leg of a cancelled pipeline.
- *Reconciler.* It gains the owner verdict before its heartbeat verdict and never judges row 1.
  - A DEAD leg of a terminal pipeline is settled to `cancelled`, which closes cancel-then-crash.
  - A DEAD leg of a running or paused pipeline is handed to the bridge through an injected callable,
    wired in `triggers/server.py` the way `status_fn` is (`server.py:593-608`). The bridge applies
    the adopt rules above, so the restart risk rules decide, not a blanket failure.
- *Owner rewrite.* The owner is rewritten (D1) whenever a process adopts, re-attaches or claims a
  leg, or resumes an approved template run.

**D5 — scheduled and on-event steps cannot fire twice and cannot be lost.**

- *What already holds.* `advance_step` re-checks `status.startswith("awaiting_")` inside the
  fleet-wide per-pipeline critical section and puts the record there
  (`pipeline_bridge.py:2133-2153`, `:2195`). Concurrent fires on N replicas, and replays after the
  step moved on, are already refused.
- *The gap* is a crash between `_submit_step` (`:2186`) and that put. The step still reads
  `awaiting_*`, and adopt re-arms it (`:420-427`).
- *The decision.* `StepState` gains `fired_key: Optional[str] = None`, an additive field. Inside the
  critical section and before `_submit_step`, the bridge writes
  `fired_key = "<trigger>@<fire time, UTC, second precision>"` and puts the record (write-ahead).
  APScheduler does not pass the scheduled fire time to `_auto_advance` (`pipeline_bridge.py:2437-2440`),
  so the bridge derives it from the trigger's cron and the current time. No event id is needed.
  The key only records "a fire began", so the `Message` event contract is unchanged.
- *Scope of the key.* `fired_key` belongs to the current wait. `_mark_awaiting`
  (`pipeline_bridge.py:2400-2402`) clears it, so a loop iteration's earlier fire never parks a step
  that has not fired.
- *On adopt,* an `awaiting_schedule`/`awaiting_event` step with `fired_key` set is handled this way:
  - If its run is found by #2540's claim, now also scanning these statuses and the run's
    `step_id`, the step takes it.
  - Otherwise the step is parked `awaiting_advance` with `leg_parked_after_restart`.

  It is never re-armed silently, and never left waiting forever. A fast leg that finished before the
  crash is not found by the claim scan, which sees only non-terminal runs, so it is parked for a
  person. That is the same "may already have run" trade #2540 makes.

**D6 — leftovers from before #2554 are cleaned once, by a script.**
`scripts/ops/settle_dangling_runs.py` (a new directory, no deployment names, per ADR-0124 D5) lists
the `queued`/`running`/`awaiting_approval` child Runs of terminal pipelines.
- It settles only LEGACY runs (no owner; every pre-#2554 leftover is one). It skips ALIVE and
  UNDETERMINED runs (D3), because, as a one-shot pass, it never sees DEAD. It never touches a
  finalizer's run.
- It pages past the store's list cap.
- With `--apply` it settles each to `cancelled`, with a run event that states the reason.
- It is a dry run by default, uses the `RunStore` and pipeline-store APIs (SQLite and Postgres),
  and is not a CLI verb. The owner runs it on production in the deploy window.

## Verification (verify-upstream, 2026-10-14)

- **Postgres session advisory locks** release when the session ends, and `pg_try_advisory_lock`
  never blocks. The codebase already relies on both (`core/leadership.py`, the critical sections).
- **Advisory-lock key spaces.** The two-int4 form `pg_advisory_lock(int4, int4)` and the
  one-bigint form never collide. `pg_try_advisory_xact_lock` releases at transaction end.
- **Limits.** Session locks are lost under PgBouncer transaction pooling and to idle-session or
  proxy timeouts, hence D2's deployment assumptions and re-acquire loop.
- **`flock`** locks are released by the OS when the holding process exits, including on
  `kill -9`.
- **APScheduler** does not hand the job its scheduled run time, so D5 derives the tick.
- No LangGraph, Temporal or DBOS facility applies. They own liveness for their own runs, and that
  stays as it is.

## Consequences

- `Run.owner` and `StepState.fired_key` are additive. `console/openapi.json` is unchanged, because
  `Run`, `PipelineRecord` and `StepState` are not response schemas there; verify by regeneration.
  `docs/contracts/pipelines-and-runs.md` names both fields.
- Each server and each `cairn pipeline run` process holds one extra Postgres connection for its
  owner lock: a dedicated size-1 pool, the same cost leadership already pays.
- New process-level state: `core/liveness.py`, which holds the boot id, the owner id and the owner
  lock (§5; approved with this ADR).
- Deployment assumptions, documented in the Helm and light-stack READMEs:
  - a session-mode Postgres connection (not PgBouncer transaction pooling);
  - `idle_session_timeout = 0`;
  - TCP keepalives on.
- Docstrings to correct with the implementation:
  - `reconciler.py:16-18` and `:276` (the existing conflict);
  - `protocols.py:196-200` (which legs beat);
  - `pipeline_bridge.py:1588-1590`, `:1624-1625` and `:2201-2213` (restart and cancel liveness).
- Flow legs keep their poll-loop heartbeat for the existing verdict.
- Required proof:
  - a test for each D3 row, including two processes on one host, and a CLI process next to a
    server on SQLite;
  - losing and re-acquiring the lock;
  - a Postgres restart producing no false DEAD;
  - the owner class id differs from the schema-init and run-events first keys;
  - a single-probe UNDETERMINED on adopt does not fail a live leg;
  - a probe error producing UNDETERMINED;
  - a stale `fired_key` across loop iterations;
  - re-adopt rewrites the owner;
  - a blocked loop does not make a live leg DEAD;
  - a crash between the D5 write-ahead and submit parks the step;
  - the D6 script skips ALIVE runs and finalizers, in dry run and with `--apply`.

## Alternatives rejected

- **Periodic heartbeat writes for task and template legs** (the literal reading of "owner id +
  heartbeat"). They race terminal writes on Postgres and fall silent when a leg blocks the loop.
  See Context.
- **One replica only, with leader-gated adopt.** Multi-replica pipelines would then depend on
  Temporal, which cannot run task legs yet (#1378).
- **A same-host, different-boot shortcut.** It wrongly kills live legs when two processes share a
  hostname. The per-boot lock answers the question exactly.
- **Document the limits and change nothing.** Leaves cancel-then-crash and rolling-update failures
  in place.
- **A separate leases table.** A new store, while a session lock carries the same fact with no
  writes.
