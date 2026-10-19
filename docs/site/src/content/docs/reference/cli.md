---
title: CLI reference
description: The cairn command surface — inspect, launch and drive pipelines, decide gates, manage connections and workers.
---

The `cairn` CLI is the control client. Run it from an installed wheel
(`cairn …`) or a checkout (`uv run cairn …`). `cairn --help` and
`cairn <command> --help` list every flag.

| Group | Commands |
|---|---|
| Inspect & verify | `doctor` · `ls` · `validate` · `pack` · `config` · `flow` |
| Pipelines | `pipeline` · `remote` |
| Run locally | `run` · `preflight` · `gate` · `replay` · `lineage` · `test` · `visualize` · `export` |
| Author | `init` · `lock` |
| Operate | `dev` · `worker` · `connect` · `compute` · `deploy` · `demo` |

## Inspect & verify

### `doctor`

Health check: packs load, profile bindings resolve to registered operators,
settings and their resolved values, service reachability, run-hook providers
that failed to load. `--strict` treats warnings as failures.

### `ls [PACK]`

List packs and what they contribute. `--json` for a machine-readable shape.

### `validate PATHS...`

Offline validation of a pack's pipelines, flows, templates and policies: no
model calls, no backend. A capability with no binding fails, so validate under
the profile you will run with:

```bash
CAIRN_PROFILE=examples/hello-cairn/profiles/hello.toml cairn validate examples/hello-cairn
```

### `pack`

| Subcommand | Effect |
|---|---|
| `pack schema <ref>` | the UI contract for a template: inputs, outputs, plan, steps |
| `pack check` | fail on manifest ↔ code drift in operator packs |
| `pack add` | make a scaffolded pack discoverable by the engine (wires it into the workspace's `pyproject.toml`) |
| `pack sync [dir]` | regenerate an operator pack's `pack.toml` tables and entry points from its classes; `--check` reports drift |

### `config workflow <template-ref>`

The composed config surface of a template: every reachable operator's knobs,
**governance** vs **operational**. `--json` for the raw shape. See
[Configuration](/configuration/).

### `flow`

| Subcommand | Effect |
|---|---|
| `flow ls` | list flows |
| `flow split <ref>` | show how a flow splits at in-flow approval gates |
| `flow export <ref> --to <target>` | render a flow for another engine |

## Pipelines

### `pipeline`

| Subcommand | Effect |
|---|---|
| `pipeline ls` | the installed pipelines |
| `pipeline show <ref>` | a pipeline's legs, gates and wires, and which runtime runs each leg; `--plan` for the execution plan |
| `pipeline explain <ref>` | per leg: the resolved operator or flow file, the engine, every gate and the authority demanding it, each config value and its layer, timeouts and retries. `-e <environment>`, `-i key=value`, `--json` |
| `pipeline run <ref>` | launch through the start door and drive it from this process; `-i key=value`, `-e <environment>`, `--detach` |

```bash
cairn pipeline explain hello-cairn/profile-and-publish -e sandbox
cairn pipeline run hello-cairn/profile-and-publish -e sandbox
```

Under the in-process runtime the CLI process owns the pipeline only while it
lives; `--detach` leaves it unowned (exit 2).

### `remote`

Drives a running server over its HTTP API.

```bash
cairn remote login -u dev -p dev -e http://localhost:8471     # store a token
cairn remote launch <ref> --environment sandbox --watch        # launch a pipeline
cairn remote pipelines                                          # list pipelines
cairn remote pipeline <pipeline_id>                             # one pipeline's steps
cairn remote findings <pipeline_id>                             # what its legs produced
cairn remote advance <pipeline_id> <step>                       # decide a gate / release a step
cairn remote cancel-pipeline <pipeline_id>
```

`launch` flags: `-i key=value` (repeatable), `--preset` / `--save-preset`,
`--environment`, `--watch`, `--interval`. `advance` takes `--approver` (recorded
when auth is off; under auth the server records the signed-in identity).
`cairn remote presets <ref>` and `cairn remote delete-preset` manage input
presets.

Runs: `remote submit`, `remote ps`, `remote watch`, `remote status <run_id>`,
`remote logs -f <run_id>`, `remote cancel <run_id>`, `remote approve` /
`remote reject <run_id>`. `remote hash-password` prints a password hash (the
`auth` extra). Every `remote` command takes `--endpoint` / `-e`, `--token` and
`--profile`.

## Run locally

### `run`

Fires a trigger file, or with `--input` runs a template directly, in this
process. The dev loop flags `--dry-run` (compile and show the plan),
`--mock` (operators that support it take their mock path) and
`-I, --interactive` (answer gates in the terminal) write no pipeline record.

### `preflight <template-ref>`

The side-effect-free ready/blocked verdict for a template: blockers, gates,
cost, resolved config. Exit 0 = ready, 1 = blocked. A pipeline is checked with
`cairn pipeline explain`.

### `gate <template-ref>`

Run a check workflow read-only and exit non-zero on its verdict: the CI entry
point. `--input`, `--set k=v`, `--profile`, `--fail-on fail|warn`, `--json`,
`--mock`.

### `replay <run_id>`

Render a run's persisted event trace as a timeline. `--json` for the raw
record; `--source auto|file|db`.

### `lineage [SUBJECT]`

Reads lineage records from the audit log and verifies the hash chain as it
reads. The operator that writes lineage records is part of cairn-platform; on
this install the command says so and points at `cairn replay`.

### `test [PACK_PATH]`

Run a pack's pytest suite; arguments after `--` pass through.

### `visualize template <ref>`

Render a template's compiled graph as Mermaid (`-o <file>`).

### `export <workflow>`

Render an installed workflow as one runnable pipeline file.

## Author

### `init`

| Subcommand | Creates |
|---|---|
| `init pack` | a new pack |
| `init template` | a template stub |
| `init skill` | a skill stub |
| `init policy` | a policy stub |
| `init operator <name> [--op group/name]` | an operator pack: a wheel bundling one or more operators |

### `lock`

Pin the shipped pack closure by content hash into `cairn.lock`;
`cairn lock --check` fails when it is missing or stale (a CI gate).

## Operate

### `dev`

One-command SQLite dev environment: the engine on a pinned configuration
(`~/.cairn/dev.db`, the in-process runtime and bus, auth on with `dev`/`dev`).
`--engine-only`, `--port` (default 8000), `--db <path>`, `--seed` (seed demo
records first).

### `worker`

`cairn worker --pull` claims jobs from the control plane over HTTP (see
[Workers](/features/workers/)); without `--pull` it runs a Temporal worker
(`--remote`, `--dataset`, `--task-queue`).

### `connect`

Typed connections (model APIs, compute, MCP servers). Secret values live in a
secret store; a connection holds references only.

| Subcommand | Effect |
|---|---|
| `connect add <provider>` | add a connection: `--name`, `--set k=v`, `--secret KEY=REF`; `--preset <name>` for an MCP preset; `--via oauth` for a broker |
| `connect presets` | the MCP connection presets (GitHub, Slack, Notion) |
| `connect providers` | the provider catalog |
| `connect ls` | configured connections (names, kinds, secret keys; never values) |
| `connect test <name>` | resolve the secret refs and run the provider's authenticated health probe |
| `connect authorize <name>` | restart the OAuth flow of an OAuth connection |
| `connect rm <name>` | delete a connection (never the secret value) |

### `compute`

`compute status [connection]` and `compute sweep` (stop instances whose lease
expired; report-only without `--apply`).

### `deploy doctor`

Check that a deployment `.env` and the enabled compose profiles agree:
`--env <file>`, `--profile <name>` (repeatable), `--strict`.

### `demo`

Seed demo records into a workspace (`-w`, default `demo`); `--archive`
tears it down (a soft delete).
