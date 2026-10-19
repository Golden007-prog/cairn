---
title: A nightly job on your own box
description: A cron-started pipeline that checks a secret, runs a report over SSH, carries it through a flow, proposes the follow-up to a person, audits it, and leaves receipts from a run hook.
---

`nightly-ops` is the operations pipeline of `hello-cairn`. It runs every night
without a person starting it, and still asks one before anything follows from
it.

```
job (template: secret check → SSH report) → summarise (flow) → propose ⏸ → audit
```

Features used: [triggers](/features/triggers-and-events/#schedule),
[remote execution and secrets](/features/remote-and-secrets/),
[dataflow](/features/dataflow-and-artifacts/),
[monitor and run hooks](/features/monitor-and-hooks/),
[governance](/features/governance/#audit).

## 1. Install

```bash
uv sync --extra dev --extra hello            # the example wheel (its run hook), asyncssh, DBOS
export CAIRN_PROFILE=examples/hello-cairn/profiles/hello.toml
```

The run hook fires only from the **installed** example wheel; a dev pack
loaded through `CAIRN_PACK_PATH` has no entry points.

## 2. The secret and the box

The job checks that a secret exists (never reading it into the run) and runs a
command on an SSH box. Both are set up outside the pipeline:

```bash
export HELLO_REPORT_TOKEN=...                    # the secret the job checks for
export HELLO_SSH_KEY="$(cat ~/.ssh/id_ed25519)"  # in the ENGINE's environment
CAIRN_STORAGE=sqlite:///$HOME/.cairn/dev.db \
cairn connect add ssh --name hello-ssh --set host=<box> --set user=<user> \
  --set known_hosts=$HOME/.ssh/known_hosts --secret PRIVATE_KEY=HELLO_SSH_KEY
```

The connection goes into the engine's store (`cairn dev` uses
`~/.cairn/dev.db`). Its secret is an env var **name** the engine reads. The box
needs `sh`, an SFTP subsystem, and `setsid` (Linux) or `perl` (macOS).

`profiles/hello.toml` binds `secrets.store` to `secrets/env`. To keep the
secret in Infisical instead, use `profiles/hello-infisical.toml` (see
[Infisical](/features/remote-and-secrets/#infisical)); nothing in the pipeline
changes.

## 3. Start the engine and run it once by hand

```bash
cairn dev --engine-only --port 8471 &
cairn remote login -u dev -p dev -e http://localhost:8471
cairn remote launch hello-cairn/nightly-ops --environment sandbox --watch
```

1. `job` runs the `nightly-job` template: a `secrets.store` capability node
   checks `HELLO_REPORT_TOKEN`, then `remote/exec` runs `command` on the box
   named by `compute_target` (default `hello-ssh`) and reads back the
   `metrics.json` it wrote.
2. `summarise` carries the report through the builtin dataflow engine (`noop`,
   then `noop.slow`).
3. `propose` (`monitor/propose`) files the follow-up, here "run
   `profile-and-publish`", and waits for a person:

   ```bash
   cairn remote advance <pipeline_id> propose
   ```

4. `audit` (`audit/store`) records `hello.nightly.reported` with the box's
   host as the target.

## 4. Let the schedule run it

The pipeline's cron is the one trigger `hello-cairn` ships switched on:

```yaml
# examples/hello-cairn/pipelines/nightly-ops.yaml
triggers:
  - kind: schedule
    cron: "30 2 * * *"
    misfire: skip
    enabled: true
    description: "Every night at 02:30 (engine time)."
```

The engine arms it at boot in the `default` workspace. At 02:30 engine time a
run starts through the same launch door as a manual launch, with the same
budget (`usd: 0.1`), series and approvers, and stops at `propose` for a
person.

## 5. The receipts

With the example wheel installed, the `hello-receipts` run hook appends one
JSON line per settled leg and finished run to
`$CAIRN_HOME/hello-cairn/receipts.jsonl` (default `~/.cairn`):

```bash
tail -n 5 ~/.cairn/hello-cairn/receipts.jsonl
```

Write your own listener the same way: an async `(event, payload) -> None`
under the `cairn.run_hooks` entry point of a package you install.

## What to check

- `cairn remote findings <pipeline_id>`: the host, the report, whether the
  proposal was approved, and the audit hash.
- The audit log has a `hello.nightly.reported` row.
- The receipts file has a line for each of the four legs.

## Limits

- The pipeline's `table` watch on `hello_events` is declared but not armed on
  the public core.
- An approved proposal is recorded (`approved`); no leg in this pipeline
  launches the proposed workflow. `monitor/dispatch` is the operator that
  would.
