---
title: Packs
description: The pack contract — pipelines, flows, templates, policies, manifests — and the example pack.
---

A **pack** is declarative domain knowledge: markdown + YAML/TOML assets
described by a `pack.toml` manifest, plus a thin `__init__.py` entry-point
shim that registers the pack with the engine. Its runnable surface is
`pipelines/*.yaml`; the flows and templates those pipelines name live in
`flows/`. Executable code lives in [operators](/concepts/operators/), never
in domain packs: a domain pack is data plus a little glue (a run hook, a stdio
MCP server), and neither piece of glue registers an operation.

A pack is found as an installed wheel through its `cairn.packs` entry point,
or as a dev directory on `CAIRN_PACK_PATH`. Assets are found by convention
(`pipelines/`, `flows/`, `rag/`, `data/`, `policies/`).

## The manifest (`pack.toml`)

- `[pack]` — `name`, `version`, `description`, `tags`, plus classification:
  `kind` (`domain` / `operator` / `service_provider` / `knowledge`),
  `publisher`, `trust_level` (`builtin` / `official` / `community` /
  `private` / `local`).
- `[assets]` — optional; relative paths to `templates`, `prompts`, `skills`,
  `policies`, `schemas`, `triggers`, `evals`, `rag`, `memory`, `examples`,
  `data`.
- `[requires.*]` — what the pack needs from the deployment, e.g.
  `[requires.rag.<name>]` (a corpus) and `[requires.mcp]` (MCP servers and
  tools).
- `[provides]` — what it contributes: templates, capabilities, policies,
  triggers.
- `[operator."<pack>/<name>"]` — for operator packs, one table per operator:
  schemas, runtime flags, `risk`, `executors`, `requires_approval`.
- `[demo.*]` / `[eval.*]` — tiered runnable demo payloads (easy / medium /
  complex) and eval recipes, so a pack is try-able and scorable out of the
  box.

## Templates — three flow types

A template is a markdown file with YAML frontmatter that compiles to a graph:

| `flow.type` | Shape |
|---|---|
| `prompt` | one LLM call with structured output |
| `agent_loop` | a ReAct-style tool-using agent |
| `state_machine` | a behavior tree of deterministic steps and gates, compiled to LangGraph — the flow type every flagship workflow uses |

Templates are referenced by namespaced ref: `<pack>/<template>`, e.g.
`hello-cairn/assistant`. A template runs as a leg of a pipeline
(`template: assistant`); see [Models and agents](/features/models-and-agents/).

## Triggers

Triggers belong to pipelines: a pipeline file declares how it starts besides
by hand under `triggers:` (schedules, watches, webhooks, events). Packs can
also ship per-source **trigger specs** (`triggers/*.yaml`) that declare
webhook ingress — signature verification, normalization, match rules, input
mapping. See [Triggers and events](/features/triggers-and-events/).

## Policies

Packs ship default policy YAML (e.g. "all high-risk actions require human
approval", "production rollbacks need a senior approver"). Policies gate
*actions*, not templates — see [Governance](/features/governance/).

## The example pack

The repository ships one domain pack, `examples/hello-cairn` (the wheel
`cairn-pack-hello`). Its manifest:

```toml
# examples/hello-cairn/pack.toml (abridged)
[pack]
name        = "hello-cairn"
version     = "0.2.0"
kind        = "domain"
publisher   = "cairn"
trust_level = "builtin"

[compat]
cairn_engine = ">=0.1.0"

[requires.rag.docs]
corpus = "rag/docs"
access = "vector_search"

[requires.mcp]
servers = ["hello-calc"]
tools   = ["hello-calc:word_count", "hello-calc:add"]
```

Its four pipelines are described on the [Overview](/). The domain packs for
training, serving, evaluation and incident response are part of
cairn-platform.

## Scaffolding and validation

```bash
cairn init pack my_pack          # new pack skeleton
cairn init template my-flow      # add a template stub
cairn validate ./my_pack         # offline: no LLM, no backend
cairn test ./my_pack             # the pack's pytest suite
cairn ls my_pack                 # what it contributes once installed
```

Packs install as wheels; `cairn.lock` pins the shipped pack closure by content
hash (`cairn lock --check` is the CI gate).
