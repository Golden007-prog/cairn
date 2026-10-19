---
title: HTTP API
description: The cairn server's HTTP surface — the start door, pipelines and their gates, runs, schedules, webhooks, workers and accounts.
draft: true
---

One FastAPI server (`cairn-server`, port `CAIRN_TRIGGER_PORT`, default 9090)
exposes the control plane. Interactive docs ship with it at `/docs` (OpenAPI
at `/openapi.json`).

## Conventions

- **Auth** — on when `CAIRN_JWT_SECRET` is set: `POST /token` (OAuth2 password
  form) returns a bearer JWT, and every route outside a small public list
  (`/healthz`, `/token`, `/auth/status`, the API docs, signature-verified
  webhook receivers) needs it.
- **Workspaces** — every route can be addressed per workspace as
  `/workspaces/{id}/<path>`. Cross-workspace access to a run 404s.
- **Streaming** — `GET /runs/{id}/stream` is Server-Sent Events.

## Start

| Method | Path | Purpose |
|---|---|---|
| POST | `/start` | the one launch door: `{"ref": "<pack>/<pipeline>", ...}` |

## Pipelines

| Method | Path | Purpose |
|---|---|---|
| POST | `/pipelines/launch` | launch an installed pipeline |
| GET | `/pipelines` · `/pipelines/{id}` | list / one pipeline's record |
| GET | `/pipelines/{id}/findings` · `/pipelines/{id}/config` | what the legs produced; the composed step config |
| GET | `/pipelines/installed` · `/pipelines/{pack}/{name}/explain` | installed pipelines; the explain projection |
| POST | `/pipelines/preview` · `/pipelines/preview-config` | plan without launching |
| GET/POST | `/pipelines/presets` | input presets |
| POST | `/pipelines/{id}/steps/{step}/approve` | decide a gate |
| POST | `/pipelines/{id}/steps/{step}/advance` | release a step waiting on a person |
| POST | `/pipelines/{id}/cancel` | cancel |

## Runs

| Method | Path | Purpose |
|---|---|---|
| GET | `/runs` · `/runs/{id}` | list / one run's record, with provenance |
| GET | `/runs/{id}/events` · `/runs/{id}/stream` | event log; live SSE |
| GET | `/runs/{id}/findings` · `/runs/{id}/artifacts` · `/runs/{id}/artifacts/download` · `/runs/{id}/report` | what a run produced |
| GET | `/runs/pending-approvals` · `/runs/{id}/pending-approval` | what is waiting for a person |
| POST | `/runs/{id}/resume` | `{decision, approver, note}` — a missing decision is a 422, not awaiting is a 409 |
| POST | `/runs/{id}/cancel` · `/runs/{id}/expire` · `/approvals/dismiss` | cancel; expire one gate; bulk-dismiss stale gates |
| GET/POST | `/runs/{id}/control` | run control |
| POST | `/runs/{id}/events/ingest` | a worker's report batch |

## Templates

| Method | Path | Purpose |
|---|---|---|
| GET | `/packs` | registered templates |
| GET | `/packs/{pack}/{template}/contract` · `/graph` | the UI contract; the compiled graph |
| POST | `/packs/{pack}/{template}/validate` · `/preflight` | validate inputs; the ready/blocked verdict |

## Triggers and schedules

| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/triggers/scheduled` | list / create cron schedules |
| PATCH/DELETE | `/triggers/scheduled/{id}` | pause, resume / delete |
| POST | `/triggers/scheduled/preview` | preview a schedule |
| POST | `/triggers/<path>` | a declared webhook receiver, e.g. `/triggers/hello-sample` |

## Workers

| Method | Path | Purpose |
|---|---|---|
| POST | `/jobs/claim` | a pull worker claims a job |
| POST | `/jobs/{id}/heartbeat` | hold the lease |

## Registry, accounts, workspaces

- `GET /registry/catalog` · `/registry/installed-workflows` · `/registry/installs`;
  `POST /registry/{install,uninstall,enable,disable,sync-workflows}`.
- `GET/POST /workspaces`, `PATCH/DELETE /workspaces/{id}`; `GET /variables`,
  `PUT/DELETE /variables/{key}`.
- `POST /token`, `GET /auth/me`, `GET /auth/status`, `GET /healthz`.
- `GET /oauth/callback` completes an OAuth connection (`cairn connect add --via oauth`).

The Python client `cairn-client` wraps these routes.
