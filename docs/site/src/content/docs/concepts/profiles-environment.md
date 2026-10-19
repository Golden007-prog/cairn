---
title: Profiles
description: How technology choices (profiles) decide which provider satisfies each capability a pack needs.
---

A pack declares *what* it needs; a **profile** decides *which* provider
satisfies each capability. Credentials and endpoints come from the
deployment's own configuration — run `cairn doctor` to see what's set and
reachable.

## Profile format

A profile is a TOML file with a single closed `[profile]` table — unknown
keys are a hard error, so typos fail loudly:

```toml
[profile]
extends = "hello.toml"           # one-level inheritance, cycle-checked
# attach-lists: omit = adopt all, [] = adopt none
pack_policies = ["hello-require-review-on-publish"]

[profile.bindings]               # capability -> operator (the dispatch seam)
"secrets.store" = "secrets/infisical"
"redact.text"   = "redact/text"
"audit.sink"    = "audit/store"

[profile.backends]               # cross-cutting toggles / choosers
dataflow_engine = "builtin"
ml_engine       = "builtin"

[profile.mcp_servers.hello-calc] # MCP servers available to templates
transport = "stdio"
command   = ["python", "examples/hello-cairn/tools/calc_server.py"]
```

A `[profile.config."<operator>"]` table sets per-operator config partials.

On `extends`, bindings/backends/MCP shallow-merge child-over-parent; the
per-operator `config` tables deep-merge; `policies`/`knowledge` attach-lists
replace when present.

## Shipped profiles

| Profile | Extends | Purpose |
|---|---|---|
| `local-dev` | — | zero-config dev; the default for local compiles |
| `secure` | local-dev | adds redaction at the model egress (`redact.text`) and the hash-chained audit sink (`audit.sink`) |
| `examples/hello-cairn/profiles/hello.toml` | — | what the example runs under: the builtin bindings for memory, storage, RAG, MCP and `secrets.store` → `secrets/env`, the `hello-calc` MCP server, the builtin flow engines |
| `examples/hello-cairn/profiles/hello-infisical.toml` | hello.toml | the same, with `secrets.store` → `secrets/infisical` |

The engine's profiles live in `src/cairn/profiles/`; a deployment points at a
profile by path with `CAIRN_PROFILE`. `cairn doctor` checks that every
binding resolves to a registered operator.

A profile can pin the runtime (`durable_runtime` under `[profile.backends]`);
a pin beats `CAIRN_RUNTIME`.

## Secrets

Secrets follow a reference discipline everywhere: connections and stores hold
*references*; the actual values live in the secret store (a local env-var
backend by default, Infisical optional) and are never printed.
