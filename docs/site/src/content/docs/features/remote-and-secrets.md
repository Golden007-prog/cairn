---
title: Remote execution and secrets
description: Run a job on a box you own over SSH (remote/exec on a compute.ssh connection), and read secrets through secrets.store on env or Infisical.
---

## Remote execution over SSH

`remote/exec` (`cairn-operator-remote`, capability `remote.exec`) runs a
command on a remote box over SSH, detaches it into its own session, and reads
back what it wrote. Its risk class is `external_write`.

The box is a **connection**, never something a template spells out: a
`compute.ssh` connection in the engine's connection store, named by the run.

### Setup

```bash
uv sync --extra ssh                        # asyncssh (the hello extra carries it too)

export HELLO_SSH_KEY="$(cat ~/.ssh/id_ed25519)"  # in the ENGINE's environment
CAIRN_STORAGE=sqlite:///$HOME/.cairn/dev.db \
cairn connect add ssh --name hello-ssh --set host=<box> --set user=<user> \
  --set known_hosts=$HOME/.ssh/known_hosts --secret PRIVATE_KEY=HELLO_SSH_KEY
cairn connect test hello-ssh
```

The connection goes into the engine's store, hence `CAIRN_STORAGE` pointing at
the engine's database. Its secret is a **reference**: `PRIVATE_KEY=HELLO_SSH_KEY`
names an environment variable the engine reads when the connection is used;
the value is never stored. The `ssh` provider's config keys are `host`, `user`,
`port` (default 22), `known_hosts`, `instance_id`, `region` and `hourly_usd`.

The box needs `sh`, an SFTP subsystem, and `setsid` (Linux) or `perl`
(macOS): `remote/exec` detaches the job through whichever is present.

### In a template

A template's `operation` node is where `remote/exec` gets its box
(`execution.compute_target`); a `task:` leg has no compute target.

```yaml
# examples/hello-cairn/flows/nightly-job.md (front matter, excerpt)
- type: operation
  operator: remote/exec
  id: report
  execution:
    compute_target: $input.compute_target
  input:
    command: $input.command
    env:
      HELLO_SECRET_PRESENT: $state.findings.secret_present
  output:
    status: $state.findings.job_status
    exit_code: $state.findings.job_exit_code
    host: $state.findings.job_host
    metrics: $state.findings.report
    error: $state.findings.job_error
```

The example's command writes `metrics.json` in its working directory, and
`remote/exec` reads it back as `metrics`.

## Secrets

`secrets.store` is a capability with two public implementations:

| Operator | Package | Reads from |
|---|---|---|
| `secrets/env` | `cairn-operator-secrets-env` | the engine's environment |
| `secrets/infisical` | `cairn-operator-secrets-infisical` (`[infisical]` extra) | an Infisical project |

The profile picks one; the template names only the capability:

```yaml
# examples/hello-cairn/flows/nightly-job.md (front matter, excerpt)
- type: operation
  capability: contract://secrets.store/1.0.0
  id: secret
  input:
    name: $input.secret_name
  output:
    present: $state.findings.secret_present
```

```toml
# examples/hello-cairn/profiles/hello.toml
[profile.bindings]
"secrets.store"  = "secrets/env"     # cairn-operator-secrets-env
```

```toml
# examples/hello-cairn/profiles/hello-infisical.toml
[profile]
visibility = "public"
extends    = "hello.toml"

[profile.bindings]
"secrets.store" = "secrets/infisical"
```

A capability node with no binding refuses to compile, which is why
`cairn validate examples/hello-cairn` needs the example's profile. The
example's check is a **presence** check: the value never enters the run's
state.

### Infisical

```bash
uv sync --extra infisical
export CAIRN_PROFILE=examples/hello-cairn/profiles/hello-infisical.toml
export INFISICAL_CLIENT_ID=... INFISICAL_CLIENT_SECRET=... INFISICAL_PROJECT_ID=...
export INFISICAL_HOST=...           # a self-hosted server
export INFISICAL_ENVIRONMENT=dev    # the default
```

The client id and secret belong to a universal-auth **machine identity**. The
light stack's `infisical` compose profile runs a self-hosted Infisical server
(see [Deployment](/deployment/)).

### Secret references elsewhere

Connections hold references, never values. A reference is an env var name
(`NAME` or `${env:NAME}`), `cairn://secrets#KEY` (a local Fernet-encrypted
file, `~/.cairn/secrets.db`), or `infisical://<project>/<path>#KEY`.

## Limits

- **Infisical is NOT PROVED end to end.** It needs a machine identity made by
  hand in Infisical's web UI, so the feature proof runs the env variant; the
  Infisical operator's own unit tests ship in its package.
- `vault://` references are named in the grammar but not implemented.
- `remote/exec` on SSH is proved against a key-only sshd. GPU boxes and other
  remote providers are part of cairn-platform.
