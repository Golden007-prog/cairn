---
title: Operators
description: The operator contract — typed, governed, swappable units of execution.
---

An **operator** executes one leg or template node — a dataset profile, a
storage write, a tag move, a job on an SSH box. Operators are the only things that touch tools, and
every one of them inherits the budget, policy gate, and audit automatically.

## The contract

An operator is a class implementing `Operation[I, O]`, registered under the
single entry-point group `cairn.operators` with a stable name like
`data/profile` or `artifact/promote`, and implements one or more capability
contracts (`contract://artifact.promote/1.0.0`). It declares:

- `input_schema` / `output_schema` — Pydantic models (required).
- `config_schema` — optional deployment-tunable knobs, each tagged with a
  scope: `governance` (environment-owned, lockable) or `operational`
  (workflow/run-tunable; the default for untagged fields). See
  [Configuration](/configuration/).
- Behavior flags (all default off): `long_running`, `requires_gpu`,
  `supports_cancel`, `supports_heartbeat`, `supports_mock` (a `.mock()` path
  for `--mock` runs), `side_effect_free`, `sandbox_safe`.
- Lifecycle methods: `run()` (required), plus optional `mock()`, `cancel()`,
  `heartbeat()`.

In `pack.toml`, each operator additionally declares a **risk class**
(`read_only` / `external_write` / `production_mutation` / `paid_compute`),
its **executors** (`in_process`, `mcp_tool`, `remote_job`, …), and whether it
`requires_approval` — a durable HITL gate fires before it runs.

## The bounded context

Operators get an `OperationContext` with exactly what they need: artifact
store, secret store (values by reference, never printed), metrics, cost
ledger, a resolved compute connection for remote jobs, and a logger. They
cannot read other nodes' outputs, touch graph state, spawn operators, or
reach the runtime directly — that boundary is what makes them swappable and
auditable.

## Swap by binding, not by code

A template names a *capability*; the active profile's `[profile.bindings]`
decides which operator satisfies it. `hello-cairn`'s nightly job checks a
secret through `secrets.store`, bound to `secrets/env` by one profile and to
`secrets/infisical` by another — the template is unchanged. The same seam
backs the substrate services: storage, memory, RAG, redaction, the audit sink,
secrets and MCP. See [Builtin operators](/reference/operators/).

## Real by default, honest about fallbacks

Operators degrade explicitly rather than silently: heavy backends are
optional extras with deterministic fallbacks, and outputs carry flags (e.g.
`checked=false`, `backend_used`) when a lighter path ran — including which
operators sample, stub, or no-op without their extra — so a deployment can be
configured fail-closed where it matters.

## Writing one

```bash
cairn init operator my_ops --op my/thing
```

scaffolds an operator pack (a wheel bundling one or more operators) with the
entry-point wiring, schemas, and a test skeleton in place.
