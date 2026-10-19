# ADR-0097 — Compute lifecycle as governed legs: `finalizer:` and the acquire/release pair

- **Status:** ACCEPTED
- **Date:** 2026-08-30
- **Issue:** #1787 (R1 of the no-bolt-on epic #1793)
- **Builds on:** ADR-0084 (leg grammar), ADR-0086 H5 (compute targets + caps), ADR-0039 (the cost operators), #1759/#1760 (a gated leg pauses), #1718 (`resolve_declared_compute`)

## Context

Every GPU session in the 2026-08-26→30 flagship campaign started and stopped its
box by hand with `scripts/boxes.sh`, and the rule "GPU only with the owner's
authorization" lived in the operator's memory rather than in the platform. Three
things follow from that, and all three have actually happened:

1. **Nothing governs turning compute ON.** A pipeline could dispatch to a running
   box, and the approval plane had plenty to say about the *work*, but the spend
   decision itself — power this thing on — had no leg, no gate and no record.
2. **Nothing guarantees turning it OFF.** A box left billing after a run has
   happened repeatedly; it was caught by human discipline, which is another way
   of saying it was caught late.
3. **The lease was a promise, not a mechanism.** "I'll stop it after a couple of
   hours" is enforced by whoever remembers.

A sweep of `cairn/core` confirmed there was no compute-lifecycle code anywhere:
nothing acquired, tracked or released a *persistent* box. (Per-job cloud GPU
provisioning is a different thing and already exists — `train/runpod`,
`train/vast`, `train/modal`, `train/kaggle` over the shared `RemoteJobOperator`
protocol. This ADR is about the pet g4dn with the baked unsloth env, the
adapters and the HF cache.)

## Buy vs build (researched 2026-08-30, #1787)

| Option | Model | Verdict |
|---|---|---|
| **SkyPilot** (Apache-2.0, active) | `sky.launch/stop/down`, autostop, multi-cloud, Python API | **the right rung for CATTLE compute**, and rejected *for this* — it manages its own clusters, so adopting it for our pet box means migrating the baked training env onto SkyPilot-provisioned instances AND accepting a second control plane beside cairn: the exact trade ADR-0096 rejected with Kestra. It also supplies none of the governance half (the gate, the record), which is cairn's own. |
| **dstack** | same shape plus service deployment | same second-control-plane objection |
| **build the thin lifecycle** | boto3 start/stop ≈ the 40 lines `scripts/boxes.sh` already proves, plus the governance pattern in-house | **adopted** |

Keep `provider` pluggable so SkyPilot can become a rung later, exactly the way
Ray is ADR-0081 rung 3. The cattle path already exists via the remote-job
operators; nothing here forecloses it.

## Decision

### D1 — The lifecycle is two ordinary task legs

`compute/acquire` (`risk: paid_compute`) and `compute/release`
(`risk: external_write`), in the existing `operator-compute` pack. Both take a
CONNECTION NAME; the `compute.ssh` connection gains three OPTIONAL config keys
(`instance_id`, `region`, `hourly_usd`) so it describes not just how to reach a
box but how to power it and what it costs. Absent them, a connection behaves
exactly as it did, and the lifecycle operators refuse with a cited reason rather
than improvising an instance id.

Both go through one module, `cairn.core.compute_lifecycle`, which is also what
the CLI sweep calls: a chokepoint is a function others call (#1376). `ec2_client`
is the single seam tests replace.

**Why the same pack as the cost operators, not a new one.** It is the same
domain and the same decision: the authorization gate in front of `acquire` shows
the numbers `compute/estimate` produces, and one install covers the whole story.
The cost operators stay pure and replayable — that claim was always scoped to
them, and the README now says so.

### D2 — The authorization gate is an ordinary `gate:` leg

No new gate machinery, and that is the decision. `approvers:` + `show:` on a
plain gate step placed in front of `acquire` is already everything the issue
asks for: it pauses the pipeline, it names the audience, it displays the hourly
rate and the estimate, and the approver plus the shown values land on the record.
Both existing release doors work unchanged (`cairn remote advance`,
`POST …/approve`). What was missing was never a feature — it was the *pattern*,
shipped in a YAML that a run actually uses.

### D3 — `finalizer: true` — the one genuinely new engine behaviour

A step declared `finalizer: true` runs when the pipeline reaches ANY terminal
state (done / failed / cancelled), exactly once, even if its dependency chain
never fired. It is `finally:`, as a leg. The grammar grows one word so that "the
box is released whatever happens" is a property of the plan.

**The mechanism: a two-move terminal.** The bridge decides a terminal *intent*
over the non-finalizer steps and records it on `PipelineRecord.terminal_intent`;
the record only takes on that terminal *status* once every finalizer has settled.
While the `finally:` block executes, the record honestly reads `running` —
because it is.

That ordering is the whole design, and it was chosen for one reason: **restart
safety comes free.** A pipeline that went terminal while the bridge was down is
still `running` in the store, so the existing `_adopt_in_flight` (which lists
running + paused) finds it with no new query. The obvious alternative — write
`done` first and remember the debt — would need a scan of every terminal
pipeline at every boot, on a table that only grows.

Consequences, each pinned by a test:

* **`_advance` submits no further work once an intent is set.** A deferred
  failure must not keep starting downstream legs while the teardown is in flight.
* **A finalizer caught mid-flight by a restart is RE-RUN, not parked.**
  `_resume_task_leg`'s fail-closed rule (a non-`read_only` op is not re-fired
  without a human) is right for the pipeline's own work and exactly wrong here:
  parking the teardown for a decision is a box billing until somebody notices.
  This is why **a finalizer must be idempotent** — a contract, stated here and
  satisfied by `compute/release` (stopping a stopped instance is a no-op
  success).
* **A failed finalizer stops the run reading as a clean success.** A release that
  failed means the box may still be up; a `done` there would hide the one thing
  this feature is for. A `cancelled` intent is never downgraded, though — the
  human's word wins.
* **A finalizer whose wires cannot resolve is `skipped`, not failed.** A release
  whose acquire never ran has nothing to release. (The shipped pipeline wires
  release off `$input`, not off `acquire`, so this stays a belt.)
* **Refused at parse:** a finalizer on a `gate:`/`loop:` leg (a gate nobody can
  answer after termination), inside a loop body (a pipeline terminates once), or
  carrying `when:` (a route that can skip a `finally:`). Refused at plan time:
  anything that `depends_on` a finalizer.

Every branch is guarded on the plan actually declaring a finalizer, so a
pipeline without one takes byte-identical paths to before (#1759's discipline).

R2 reuses this attribute; it is not a compute feature that happens to live in the
engine.

### D4 — `max_hours` is a lease TAG plus a sweep, and we say exactly that

`compute/acquire` stamps `cairn:release-after` (an ISO deadline) and
`cairn:managed` on the instance. `cairn compute sweep [--apply]` stops every
cairn-managed instance whose deadline has passed, scoped by that tag and by a
*readable* deadline — an unparseable tag is left alone, never guessed at.

What this buys over the status quo: the guard no longer depends on the pipeline's
own control plane surviving. The deadline lives on the instance, and the sweep
needs nothing but credentials, so any host or cron enforces every lease:

```
*/10 * * * *  cairn compute sweep --apply --region <region>
```

**What it is not:** instance-side enforcement. The sweep runs when something runs
it. True cloud-side enforcement — an EventBridge one-shot schedule targeting
`ec2:StopInstances`, or an `arn:aws:automate:…:ec2:stop` alarm action — requires
an IAM role created in the owner's AWS account, which this work deliberately does
not do on its own. It is named here as the next rung so nobody mistakes the
watchdog for the guard.

### D5 — What binds to the acquired box, stated rather than implied

`acquire` outputs `target` = the connection name. It is *not* a per-leg compute
override, because none exists: a `flow:` leg's target comes from the ENVIRONMENT
(`ml_target` / `dataflow_target`, via `_leg_target`), and a `task:` leg carries no
compute binding at all (#1459). So the managed pipeline states the contract in
the file — `compute_connection` must name the connection the environment's
`ml_target` selects — and `cairn pipeline explain` prints both sides, so a
mismatch is one command away instead of a surprise at dispatch.

Wiring `$steps.acquire.target` into a flow leg's `with:` today would be a knob
that reads as configured and does nothing, which #1675 taught us to refuse. A
per-step compute override in the leg grammar is the natural next slice.

## Rejected alternatives

* **Release as a `try/except` in the bridge's failure path.** Covers failure and
  misses cancel, misses timeout, and is invisible in the plan — the reviewer
  cannot see that a teardown exists.
* **A reaper that stops idle boxes.** Idle is not the same as finished; it stops
  a box mid-checkpoint-upload and cannot tell a paused approval from an
  abandoned run.
* **A new terminal status (`finalizing`).** Three stores, a transition table and
  every consumer's status switch, to express something `running` already says
  truthfully.
* **`risk: production_mutation` or `paid_compute` on release.** Both resolve to
  `high` in the approval plane, where HITL policies bite — a teardown paused
  behind an unanswered gate is a box billing while it waits, i.e. the exact
  failure this ADR exists to end. `external_write` records the mutation honestly
  and is never approval-gated.
* **`compute/quote` at the authorization gate.** It reads live provider prices
  off `ctx.compute`, and a pipeline task leg has no compute binding — it would
  show the approver a null. `compute/estimate` is pure, needs no connection, and
  prices exactly what is about to run.

## Consequences

* The compute decision is on the record: who authorized it, what they were shown,
  which box, for how long, and when it was released.
* One new word in the pipeline grammar, and R2 gets it for free.
* Bundle pipelines (`plan_bundle`) do not carry `finalizer` — the legacy path is
  unchanged, deliberately.
* **The live proof is deferred.** Everything here is exercised hermetically (the
  boto3 seam is stubbed; the local drive pointed `CAIRN_EC2_CLIENT_FACTORY` at a
  fake). No real EC2 instance was started or stopped by this work. The
  EC2-verified half of #1787's criteria 3 and 6 — a real box observed stopped
  after each terminal state, and a w2 flagship re-run with zero manual box
  operations — belongs to the owner's authorized GPU session.
