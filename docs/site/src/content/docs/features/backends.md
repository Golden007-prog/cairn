---
title: Backends
description: Storage (SQLite or Postgres), the event bus (in-process or Postgres) and the runtime (in-process, DBOS, Temporal) — chosen by configuration, not code.
---

Three choices decide where cairn keeps its state and how durable a run is.
Each is one environment variable (or a profile setting). The pipelines do not
change.

| Choice | Variable | Options | Default |
|---|---|---|---|
| storage | `CAIRN_STORAGE` | `sqlite:///<path>` · `postgres://…` | unset: the run store is in memory (volatile) and the other stores use `~/.cairn/cairn.db`; `cairn dev` pins `~/.cairn/dev.db` |
| bus | `CAIRN_BUS` | `inprocess` · `postgres://…` | `inprocess` |
| runtime | `CAIRN_RUNTIME` | `inprocess` · `dbos` · `temporal` | `inprocess` |

`cairn dev` pins all three (SQLite at `~/.cairn/dev.db`, the in-process
runtime and bus); to choose them, start `cairn-server` with the variables set.

The feature proof runs all four `hello-cairn` pipelines in every cell of
storage {SQLite, Postgres} × runtime {in-process, DBOS} × bus {in-process,
Postgres}.

## Storage

All of cairn's own tables (runs, pipelines, the artifact registry, schedules,
connections, and the rest) live in the one database `CAIRN_STORAGE` names.
Always set it for a server you want to keep: with it unset, runs live in
memory and are lost on restart.

```bash
export CAIRN_STORAGE=sqlite:///$HOME/.cairn/cairn.db          # one host
uv sync --extra storage-postgres
export CAIRN_STORAGE=postgres://cairn@127.0.0.1:5432/cairn    # shared
```

SQLite is single-host: a process proves it is alive with a file lock next to
the database. Use Postgres when more than one process or host touches the data.

## Event bus

```bash
export CAIRN_BUS=postgres://cairn@127.0.0.1:5432/cairn
```

| Bus | Delivery |
|---|---|
| in-process | fan-out inside one process; only a subscriber already listening sees a message |
| Postgres | LISTEN/NOTIFY plus a durable message log: an `emits` event is replayed to a step that starts waiting later |

A `redis://` value is refused at start-up; there is no Redis bus.

## Runtime

| Runtime | What you get | Needs |
|---|---|---|
| `inprocess` | the engine process runs legs and templates; a crash is handled by the restart rules ([Recovery](/features/recovery/)) | nothing |
| `dbos` | each template run is a DBOS workflow that DBOS recovers at the next boot; no extra server | the `runtime-dbos` extra |
| `temporal` | runs as Temporal workflows on `cairn-worker` processes | the `runtime-temporal` extra and a Temporal cluster |

```bash
uv sync --extra runtime-dbos
export CAIRN_RUNTIME=dbos
export CAIRN_DBOS_DSN=sqlite:///$HOME/.cairn/dbos.sqlite   # the default; or a postgres:// DSN
```

A profile can pin the runtime (`durable_runtime` under `[profile.backends]`),
and a pin beats `CAIRN_RUNTIME`. The example's profile pins none on purpose.

### Checkpointer

Paused template state (for a gate inside a template) lives in a LangGraph
checkpointer: `CAIRN_CHECKPOINTER` = `memory` (default; lost on restart),
`sqlite` (`CAIRN_CHECKPOINTER_PATH`, the `runtime-sqlite` extra) or `postgres`
(`CAIRN_CHECKPOINTER_DSN`, the `runtime-postgres` extra). The proof pairs DBOS
with a SQLite or Postgres checkpointer.

## Limits

- **DBOS on SQLite needs SQLite >= 3.42.** DBOS's system tables use
  `unixepoch('subsec')`; the engine refuses an older SQLite at boot and names
  the remedy: a Python whose SQLite is newer (a uv-managed Python bundles
  one), or a Postgres `CAIRN_DBOS_DSN`.
- **Temporal runs flow legs only.** It is installable, but under
  `CAIRN_RUNTIME=temporal` a `task:` leg fails, so the example pipelines run on
  in-process or DBOS. Temporal is not in the proof matrix.
- On the Postgres store, the owner lock needs a direct or session-mode
  connection: PgBouncer transaction pooling breaks session locks.
- The `rag-qdrant` and `rag-pgvector` vector stores are separate choices; see
  [Models and agents](/features/models-and-agents/#retrieval-rag).
