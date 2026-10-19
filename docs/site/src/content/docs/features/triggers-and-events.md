---
title: Triggers and events
description: "How a pipeline starts besides by hand — schedules, dataset watches, webhooks, a leg's emitted event, and a `kind: event` trigger."
---

A pipeline always starts by hand (`manual` is implicit). Its file can declare
more ways to start under `triggers:`. Every declared trigger starts **the
pipeline that declares it**, through the same launch door as a manual start,
so a trigger-started run is governed exactly like any other.

| Kind | Fires when | Example in `hello-cairn` |
|---|---|---|
| `schedule` | a cron expression ticks | `nightly-ops` (02:30, on); `profile-and-publish` (Monday 06:00, off) |
| `watch` (dataset) | a registered dataset gained a version | `profile-and-publish` on `hello-sample` (off) |
| `webhook` | a system POSTs to the trigger's path | `profile-and-publish` at `POST /triggers/hello-sample` (off) |
| `event` | a message arrives on a bus topic | `ask-the-docs` on `dataset.ready` (off) |

Separately, a **step** can declare `emits: <topic>` (publish when it
completes) and `trigger: on:<topic>` (wait for a topic before it runs).

## Arming

The engine arms every declared trigger when it starts: it installs each
discovered pipeline into the `default` workspace, and each trigger becomes a
row that starts its own pipeline. A trigger fires only when its declaration
says `enabled: true`. To turn one on, set it in the pipeline file and restart
the engine. Out of the box only `nightly-ops`' cron is on.

## Schedule

```yaml
# examples/hello-cairn/pipelines/nightly-ops.yaml
triggers:
  - kind: schedule
    cron: "30 2 * * *"
    misfire: skip
    enabled: true
    description: "Every night at 02:30 (engine time)."
```

Schedules tick in the engine process (engine time).

## Dataset watch

```yaml
# examples/hello-cairn/pipelines/profile-and-publish.yaml
- kind: watch
  source: {kind: dataset, ref: hello-sample}
  every: "*/15 * * * *"
  on_change: start
  enabled: false
  description: "Re-check when the hello-sample dataset gets a new version."
```

`every:` is how often the watch looks. Each tick checks whether
`hello-sample` gained a registered version and starts the pipeline only when it
did: no change, no run. `on_change: start` lets a change start the pipeline
without a person; `propose` (the default) files an approval card instead. A
version is registered by a flow leg's `noop` stage (see
[Dataflow and artifacts](/features/dataflow-and-artifacts/#dataset-versions)).

## Webhook

```yaml
# examples/hello-cairn/pipelines/profile-and-publish.yaml
- kind: webhook
  source: custom
  path: /triggers/hello-sample
  enabled: false
  description: "Start a check from any system that can POST JSON."
```

`source:` names a receiver preset, or `custom` with an explicit `path:`. A
receiver without a signature block is behind the same bearer-token auth as the
rest of the API.

## Events: `emits` and `kind: event`

```yaml
# examples/hello-cairn/pipelines/profile-and-publish.yaml
- id: publish
  label: Publish the approved summary
  task: storage/put
  depends_on: [promote]
  emits: dataset.ready
  with:
    store: hello-published
    key: summary
    content: $steps.redact.text
    content_type: text/plain
```

When `publish` completes, the bus carries `dataset.ready` with the leg's
output as the payload. A whole pipeline can start on that topic:

```yaml
# examples/hello-cairn/pipelines/ask-the-docs.yaml
triggers:
  - kind: event
    topic: dataset.ready
    enabled: false
    description: Ask the docs again whenever the sample dataset is published.
```

Turned on, each published `dataset.ready` starts one `ask-the-docs` run. A
step can also wait for a topic inside a pipeline with `trigger: on:dataset.ready`
(`trigger:` also takes `manual` and `schedule:<cron>`).

## Setup

Nothing beyond the engine. Event delivery depends on the bus (see
[Backends](/features/backends/)):

- **in-process bus** (default) — only a step already waiting sees an event;
- **Postgres bus** (`CAIRN_BUS=postgres://…`) — an `emits` event is durable
  and replayed to a step that starts waiting later.

## Limits

- Declared triggers arm in the `default` workspace only, and every one ships
  `enabled: false` except `nightly-ops`' cron.
- The `kind: event` listener runs on the engine's leader. A message published
  while no leader is subscribed starts nothing, on either bus.
- A pipeline may not start on a topic its own legs emit.
- `table` and `s3` watches are declared but **not armed** on the public core
  (they need a governed import that is part of cairn-platform). `nightly-ops`
  declares a `table` watch for this reason; it stays unarmed.
- A metric watch (`signal:` + `when:`) is named as unarmed rather than armed.
