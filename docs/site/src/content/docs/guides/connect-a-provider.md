---
title: Connect a provider
description: Point cairn at a model endpoint, register connections to the systems your pipelines reach, test them, and keep the secret values out of the record.
---

The sandbox needs nothing connected, and neither does the example's data
pipeline. To run pipelines that call a model or reach another system, give the
engine what it needs: a model endpoint, connections, and the secrets behind
them.

## A model endpoint

Model calls go to an OpenAI-compatible endpoint. Set one of these in the
**engine's** environment (for `cairn dev`, the shell you start it from; for the
Docker Compose stack, `deploy/light/.env`):

| Setting | Use |
|---|---|
| `OPENAI_API_KEY` | OpenAI itself |
| `OPENAI_BASE_URL` | any other OpenAI-compatible server, such as vLLM or Ollama |
| `LLM_GATEWAY=proxy`, `LLM_GATEWAY_URL`, `LLM_GATEWAY_API_KEY` | a LiteLLM gateway in front of many providers; the Compose stack's `llm-gateway` profile runs one |

Every model call is built in one place, which redacts the prompt (when the
profile binds `redact.text`) and records the call's cost against the run.

## Connections

A **connection** is a named, typed reference to an external system: a model
API, a compute target, an SSH host, an MCP server. It never holds a secret
value. A secret is an environment variable **name** the engine reads, or an
entry in your secret store.

`cairn connect` manages connections in the engine's store, so run it with the
same `CAIRN_STORAGE` as the engine:

```bash
cairn connect providers            # what you can add, and each one's secret keys
cairn connect add openai           # a connection that reads OPENAI_API_KEY
cairn connect add openai --name openai-eu --secret API_KEY=OPENAI_EU_KEY
cairn connect ls                   # names, kinds and secret keys, never values
cairn connect test openai          # authenticate now and store the result
cairn connect rm openai-eu         # removes the reference, never the secret
```

`cairn connect add` saves the connection, then runs its health check once.
`cairn connect test` exits `0` when the provider answered and `1` when the
secret is missing or the check failed. `cairn connect presets` lists the
shipped MCP connection presets (GitHub, Slack, Notion), added with
`cairn connect add --preset <name>`.

### Example: an SSH host

The example's `nightly-ops` pipeline runs a report on a machine you own, over
SSH, through a connection named `hello-ssh`:

```bash
export HELLO_SSH_KEY="$(cat ~/.ssh/id_ed25519)"    # in the ENGINE's environment
CAIRN_STORAGE=sqlite:///$HOME/.cairn/dev.db \
cairn connect add ssh --name hello-ssh --set host=<box> --set user=<user> \
  --set known_hosts=$HOME/.ssh/known_hosts --secret PRIVATE_KEY=HELLO_SSH_KEY
```

The connection records the variable name `HELLO_SSH_KEY`; the key itself
never enters the record. `remote/exec` needs the `ssh` extra.

## Using a connection

Pipelines and flows refer to a connection **by name**, and its credential is
resolved only when the leg runs. The run record carries only the name.

## Secrets in a secret store

Operators read secrets through the `secrets.store` capability, so where they
live is the environment's choice:

| Operator | Reads from |
|---|---|
| `secrets/env` | the engine's environment variables |
| `secrets/infisical` | an Infisical project (the `infisical` extra); set `INFISICAL_CLIENT_ID`, `INFISICAL_CLIENT_SECRET`, `INFISICAL_PROJECT_ID`, and `INFISICAL_HOST` for a self-hosted server |

The example ships both bindings: `profiles/hello.toml` uses `secrets/env`, and
`profiles/hello-infisical.toml` uses Infisical with nothing else changed. The
Docker Compose stack's `infisical` profile runs an Infisical server for you.

The console's integrations page, and the operators that train or serve on GPU
providers, are part of cairn-platform.

## Next

- [Capabilities and environments](/concepts/capabilities/): how operators pick up a provider.
- [Run your first pipeline](/guides/first-pipeline/): launch, follow and read a pipeline.
- [Set up your team](/guides/team/): sign-in and who decides gates.
