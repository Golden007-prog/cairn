# ADR-0126 — A leg's owner is rewritten when a process takes over running it, not when it claims it

- **Status:** ACCEPTED (owner: "Keep it + short amendment", 2026-10-14)
- **Date:** 2026-10-14
- **Issue:** #2555. **Amends** ADR-0125 D1, one sentence. ADR-0125 is accepted and is not
  edited; everything else in it stands.

## Context

ADR-0125 D1 says `Run.owner` "is rewritten, from a fresh read inside the per-pipeline critical
section, whenever a process adopts, re-attaches or claims the leg". Implementing it showed that
rewriting at *claim* time is wrong in two cases:

- A LEGACY template run (no owner, from before ADR-0125) whose process died. Claiming it on
  restart and stamping this process's owner would make it ALIVE to every later verdict
  (`runtime/reconciler.py` `owner_verdict`, row 3), although nothing executes it. It would
  never be settled.
- A leg whose owner is another live process. A booting replica claims the step's run id to
  re-map it (`dispatch/pipeline_bridge.py` `_claim_unrecorded_leg_runs`). Stamping its own owner
  there would steal the leg from the process that is running it, and the real owner's lock
  would no longer vouch for it.

## Decision

**D1 — the owner follows execution.** `Run.owner` is written when a Run is created for an
in-process leg, and rewritten only when a process actually takes over running or watching the
leg:

- re-attaching a flow leg through its engine handle;
- re-arming a queued leg's slot wait;
- re-firing a read_only task leg, which is a new Run;
- resuming an approved template run on the in-process runtime.

In the bridge the rewrite happens from a fresh read inside the per-pipeline critical section
(`pipeline_bridge.py` `_take_leg_ownership`). `dispatcher.py` `resume_run` is reached through
the run door, which is not pipeline-scoped: it rewrites after its own fresh read of the run,
without a pipeline section. That is safe because a template run never stamps a heartbeat, so
no concurrent read-modify-write races the owner write.

**D2 — claiming never rewrites.** Claiming a step's unrecorded run (`_claim_unrecorded_leg_runs`)
and re-mapping an ALIVE or UNDETERMINED leg leave `owner` as it is. A LEGACY leg stays LEGACY and
keeps the #2540 and #2555 rules; a leg owned elsewhere keeps its owner.

## Consequences

- ADR-0125 D3 row 6 (LEGACY) and D4 "an ALIVE or UNDETERMINED leg owned by another process is
  only re-mapped" hold exactly as written; this record removes the one sentence that contradicted
  them.
- A process that re-maps a leg it does not run never appears as its owner, so the reconciler's
  verdict on that leg is about the process that runs it.
