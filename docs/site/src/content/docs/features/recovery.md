---
title: Recovery
description: What a restart does to a pipeline — a gate that keeps waiting, a dead in-process leg found and failed closed, a DBOS template run resumed.
---

When the engine dies and starts again on the same store, every pipeline it was
driving is picked up. The rule throughout: **nothing that may already have
happened is done twice.** A leg keeps its one run, and a write after a lost leg
never fires on a guess.

## What happens, case by case

| Where the engine died | After the restart | Proved by |
|---|---|---|
| a person gate was waiting | the gate is still waiting and is decided through the same door; every leg before and after it has exactly one run; the published artifact is written once | the `crash.gate` cells (all 8 storage × runtime × bus cells) |
| in the middle of a flow leg (in-process) | the leg is found dead, re-attached, and settles `failed`; the pipeline fails closed; nothing is re-submitted | the `crash.mid-leg` cells |
| in the middle of a template leg (DBOS) | DBOS recovers the workflow at the next boot and the leg finishes | the `crash.dbos-resume` cell |

A step parked `awaiting_approval` has no process to lose, which is why a gate
simply survives.

## How a dead leg is found (in-process)

Every in-process leg's run names its **owner**: the process that runs it. A
process proves it is alive by holding a lock for its whole life: a session
advisory lock on Postgres, an OS file lock beside the database on SQLite.

After a restart:

1. The new process's adopt pass probes the old owner's lock. Free **once** is
   undetermined: the leg is only re-mapped, never taken over.
2. The reconciler's sweep probes again more than one re-acquire interval later.
   Free **twice** is dead, and the leg is handed to the pipeline bridge.
3. A flow leg with an engine handle is **re-attached** (its owner is rewritten)
   and watched through its engine. The builtin engine's job table died with
   the old process, so the re-attached poll fails and the leg settles `failed`.

There is no clock arithmetic: a Postgres restart or failover drops every owner
lock at once, and live processes re-acquire theirs within one interval, so
their legs are never declared dead.

## Under DBOS

A template leg under `CAIRN_RUNTIME=dbos` is the runtime's, not the bridge's:
its run is a DBOS workflow with no in-process owner, and DBOS recovers pending
workflows when it launches at the next boot. See
[Backends](/features/backends/#runtime) for the DBOS setup.

## Setup

Nothing to switch on. Two knobs tune the reconciler: how often it sweeps
(`CAIRN_RECONCILE_SWEEP_SECONDS`, default 60) and how young a run it leaves
alone so a healthy run is not raced to a false terminal
(`CAIRN_RECONCILE_GRACE_SECONDS`, default 120). The crash tests set them to 2
and 0 to run quickly. For gates inside a **template** to survive
a restart, use a durable checkpointer (`CAIRN_CHECKPOINTER=sqlite` or
`postgres`).

## Limits

- A mid-leg crash on the builtin engine **fails** the leg; it does not resume
  it. The pipeline stops rather than guessing.
- A scheduled or event-started step fires once across a crash; this is proved
  by unit tests only, no crash cell kills a scheduled step.
- On Postgres the owner lock needs a direct or session-mode connection,
  `idle_session_timeout = 0`, and no proxy that idles out a quiet connection.
- `flock` on an NFS-backed volume is not reliable; keep a SQLite store on local
  disk.
- Temporal is not part of the crash proof.

See the [recovery guide](/guides/recovery/) for a walk through each case.
