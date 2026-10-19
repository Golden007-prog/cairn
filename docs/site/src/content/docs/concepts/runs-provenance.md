---
title: Runs & provenance
description: What a run records, where it's stored, and how replay works.
---

The **run** is cairn's unit of account. Everything — a CLI invocation, a
webhook, a schedule tick, a pipeline step — becomes a run with the same
lifecycle: `queued → running → done / failed / cancelled / awaiting_approval`
(plus `expired` for gates nobody answered).

## What a run records

Every run persists, in the run store:

- **Identity & lifecycle** — id, trigger, lane, status, timestamps,
  workspace, dedup fingerprint.
- **Provenance** — the *validated* input values (secrets by reference), the
  environment, a `schema_hash` pointing into the content-addressed schema
  store, and `resolved_operators`: exactly which operator (with risk and
  approval flags) each capability resolved to under that environment. A run
  is explainable after the fact without guessing.
- **Results** — light summaries plus `result_payloads`, the full structured
  outputs (HITL-approved runs keep theirs too — the resume path persists the
  final payload the same as a straight-through run).
- **Cost & observability** — `{llm_usd, gpu_usd, storage_usd, total_usd}`, a
  cost baseline for delta views, the OTel trace id, the config preset used,
  failure type, and final verdict.
- **Event log** — an append-only, monotonically-sequenced event stream per
  run, live-streamable over SSE (`GET /runs/{id}/stream`, resumable with
  `?after_seq=N`).

Local `cairn run` invocations additionally write an on-disk trace:
`.cairn/runs/<run_id>/meta.json` + `events.jsonl`.

## Storage tiers

The run store backend is a deployment choice:

| Tier | When | Guarantee |
|---|---|---|
| in-memory | nothing configured | dev only; runs lost on restart |
| SQLite | a SQLite path is configured | restart-safe, single node |
| Postgres | a Postgres URL is configured | the production tier; also backs the config-family stores (policies, schedules, connections, presets, pipelines) |

## Replay

```bash
cairn replay <run_id>            # human-readable timeline
cairn replay <run_id> --json     # raw meta + events (+ payloads from the DB)
```

`--source auto` reads the local file trace when present, else the DB store —
so server-dispatched runs replay from anywhere with DB access. Replay is
read-only rendering of what happened; it never re-executes.

## Pipelines

A pipeline record ties its legs together: each step's state, the run id of
each task, flow and template leg, every gate's verdict and approver, and the
budget. `cairn remote pipeline <id>` shows it and `cairn remote findings <id>`
shows what the legs produced. Content-addressed lineage records across runs
are part of cairn-platform.

## Workspaces

Runs (and schedules, policies, config, audit) are scoped to a **workspace** —
the tenancy boundary. The API addresses it in the path
(`/workspaces/{id}/runs/...`), and cross-workspace access to a run 404s rather
than leaking existence. Declared triggers arm in the `default` workspace.
