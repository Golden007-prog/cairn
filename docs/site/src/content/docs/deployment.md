---
title: Deployment
description: The light compose stack (engine, Caddy, Postgres) with its add-on profiles, turning auth on for a team, and the Helm chart.
---

## The light stack

`deploy/light` runs the engine image built from this repository behind Caddy,
with Postgres for runs, checkpoints and feature flags. No accounts, no keys
and no `.env` are needed to start it.

```bash
git clone https://github.com/cairndev-sh/cairn && cd cairn/deploy/light
docker compose up -d --build
```

The first build compiles the engine image (several minutes). When
`docker compose ps` shows `server` as healthy, the API answers on
`http://localhost:8080`:

```bash
curl -s http://localhost:8080/healthz
cairn remote ps --endpoint http://localhost:8080
```

The light stack runs the `secure` profile: every run leaves a hash-chained
audit row, and text is redacted before it reaches a model. A pipeline with a
model call needs a provider key: put `OPENAI_API_KEY=sk-…` in
`deploy/light/.env` and run `docker compose up -d` again.

## Add-on profiles

Enable with `--profile <name>`, or `COMPOSE_PROFILES=llm-gateway,backup` in
`.env`.

| Profile | Adds | Needs |
|---|---|---|
| `llm-gateway` | a LiteLLM proxy (`litellm-config.yaml` beside the compose file): every governed model call through one door | `LITELLM_MASTER_KEY` set before you enable it; then `LLM_GATEWAY=proxy`, `LLM_GATEWAY_URL=http://litellm:4000`, `LLM_GATEWAY_API_KEY=<that key>` |
| `backup` | a `pg_dump` of the app database every `CAIRN_BACKUP_INTERVAL_SECONDS` (default daily), keeping `CAIRN_BACKUP_KEEP` (default 14) | nothing |
| `infisical` | a self-hosted Infisical server on `127.0.0.1:8085` for `secrets/infisical` | `INFISICAL_ENCRYPTION_KEY`, `INFISICAL_AUTH_SECRET` and `INFISICAL_DB_PASSWORD` in `.env.infisical` (not `.env`); then a machine identity made in its web UI, and the engine's `INFISICAL_*` settings |

Keep the Infisical keys in `.env.infisical`: everything in `.env` reaches the
engine container.

## Feature flags

`CAIRN_LIGHT=on` applies the `light` preset. `CAIRN_FEATURE_<NAME>=on` turns one
feature back on; `CAIRN_LIGHT=off` turns everything on.

## Auth for a team

The API is open while no JWT secret is set: fine on a laptop, never on a
reachable box. To put the stack in front of other people, set in `.env`:

```
CAIRN_JWT_SECRET=<openssl rand -hex 32>
CAIRN_API_USER=admin
CAIRN_API_PASSWORD=<choose one>
```

and run `docker compose up -d`. Every API call then needs a token:

```bash
cairn remote login --endpoint http://localhost:8080
```

With auth on, routes outside a small public list (`/healthz`, `/token`,
signature-verified webhook receivers) need a bearer token from `POST /token`.

## Ops

| Task | Command (from `deploy/light`) |
|---|---|
| Status | `docker compose ps` |
| Logs | `docker compose logs -f server` |
| Stop | `docker compose down` (keeps the volumes) |
| Reset | `docker compose down -v` (drops runs, flags, backups) |
| Update | `git pull && docker compose up -d --build` |
| Change the port | `CAIRN_PORT=9000 docker compose up -d` |

`cairn deploy doctor --env <file>` checks that a deployment `.env` and the
compose profiles you enable agree before you start anything.

## Kubernetes

For a cluster with TLS, backups and the durable runtimes, use the Helm chart
in `deploy/helm/cairn`.

## Choosing backends

The stores, the bus and the runtime are environment variables on the engine;
see [Backends](/features/backends/).
