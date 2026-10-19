---
title: Surviving a crash
description: Kill the engine at a gate, in the middle of a flow leg, and in the middle of a template run, and see what each runtime does after the restart.
---

This guide kills a real engine three times and reads the records afterwards.
It follows the repository's crash tests (`tests/e2e/hello/test_crash.py`),
which run the same steps. The rules behind each outcome are on the
[Recovery](/features/recovery/) page.

## Start an engine you can kill

`cairn dev` pins the in-process runtime and a fixed store, so start
`cairn-server` directly with the backends you want. From the repository root:

```bash
uv sync --extra dev --extra hello --extra rag
export CAIRN_PROFILE=examples/hello-cairn/profiles/hello.toml
export CAIRN_PACK_PATH=examples/hello-cairn:tests/e2e/hello/crash-pack
export CAIRN_STORAGE=sqlite:///$HOME/.cairn/crash.db       # or postgres://…
export CAIRN_RUNTIME=inprocess                              # dbos for case 3
export CAIRN_TRIGGER_PORT=8471
export CAIRN_RECONCILE_SWEEP_SECONDS=2                      # sweep often (the tests do)
export CAIRN_RECONCILE_GRACE_SECONDS=0
cairn-server & echo $! > /tmp/cairn.pid
```

With no `CAIRN_JWT_SECRET` the API is open, so `cairn remote` needs no login;
pass `-e http://localhost:8471` to each command. The crash pack adds
`hello-crash/slow-publish`: `profile-and-publish`'s legs with a flow leg slow
enough (about 12 seconds) to die inside.

## 1. Killed at a person gate

```bash
cairn remote launch hello-crash/slow-publish --environment sandbox --watch -e http://localhost:8471
# ... the pipeline pauses at `review`
kill -9 "$(cat /tmp/cairn.pid)"
cairn-server & echo $! > /tmp/cairn.pid
cairn remote pipeline <pipeline_id> -e http://localhost:8471      # `review` is still waiting
cairn remote advance <pipeline_id> review -e http://localhost:8471
```

**What you see.** The gate survived: a step parked `awaiting_approval` has no
process to lose. After the decision the pipeline finishes, every leg before
and after the gate has exactly one run, and the published artifact is written
once (one file each under `crash-staging` and `crash-published`).

## 2. Killed in the middle of a flow leg (in-process)

```bash
cairn remote launch hello-crash/slow-publish --environment sandbox -e http://localhost:8471
# within a few seconds, while `slow` is running:
kill -9 "$(cat /tmp/cairn.pid)"
cairn-server & echo $! > /tmp/cairn.pid
cairn remote pipeline <pipeline_id> -e http://localhost:8471      # watch it for a minute or two
```

**What you see.** At first the leg is only re-mapped: its old owner's lock was
found free once, which proves nothing. On a later sweep, more than one
re-acquire interval apart, the lock is free again: the owner is dead. The leg
is re-attached to its engine, but the builtin engine's job table died with the
old process, so the poll fails and the leg settles `failed`. The pipeline is
`failed`. Nothing was re-submitted, `stage` never ran, and nothing was written
to `crash-staging`.

This is the intended outcome: a leg that may or may not have done its work is
never run a second time on a guess.

## 3. Killed in the middle of a template run (DBOS)

```bash
uv sync --extra dev --extra hello --extra rag --extra runtime-dbos
export CAIRN_RUNTIME=dbos
export CAIRN_DBOS_DSN=sqlite:///$HOME/.cairn/dbos.sqlite       # needs SQLite >= 3.42
export CAIRN_CHECKPOINTER=sqlite CAIRN_CHECKPOINTER_PATH=$HOME/.cairn/ckpt.sqlite
# plus the model, embedder and MCP settings ask-the-docs needs (see the data-to-assistant guide)
cairn-server & echo $! > /tmp/cairn.pid

cairn remote launch hello-cairn/ask-the-docs --environment sandbox -e http://localhost:8471
# while the `assistant` leg waits on its model call:
kill -9 "$(cat /tmp/cairn.pid)"
cairn-server & echo $! > /tmp/cairn.pid
```

**What you see.** Under DBOS a template leg's run is a DBOS workflow, not an
in-process leg. DBOS recovers pending workflows when it launches at the next
boot, so the `assistant` leg finishes after the restart (its model call is made
again) and the pipeline moves on to `length` and `review`.

The crash test makes the model call slow on purpose (its keyless model stub
holds each answer) so there is time to kill the server inside it.

## Per runtime, in short

| Runtime | Gate | Mid flow leg | Mid template leg |
|---|---|---|---|
| in-process | survives, decided after the restart | found dead, failed closed, nothing re-submitted | the in-process restart rules apply |
| DBOS | survives | the in-process rules apply to flow legs | DBOS resumes the workflow |
| Temporal | not part of the crash proof | | |

## If it does not behave like this

- **DBOS refuses to start** with a SQLite error: your Python's SQLite is older
  than 3.42. Use a uv-managed Python or a Postgres `CAIRN_DBOS_DSN`.
- **A Postgres-backed engine declares live legs dead**: the owner lock needs a
  direct or session-mode connection (no PgBouncer transaction pooling),
  `idle_session_timeout = 0`, and no proxy that idles out quiet connections.
