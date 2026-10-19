# ADR-0131 — A declared trigger starts the pipeline that declares it: a public `dataset` watch and a `kind: event` trigger

- **Status:** ACCEPTED 2026-10-15 by the owner ("go with 1 & 1": a public dataset watch, and a
  pipeline-level event trigger, in one record).
- **Date:** 2026-10-15
- **Issues:** #2612 (a source watch always fires the platform's `watch-source`), the
  `dataset.ready` consumer left open by #2605.
- **Amends** ADR-0101 D4 (ingestion is a trigger kind: every source watch fires
  `dataset-readiness/watch-source`) for installs without that pipeline. **Extends** ADR-0101 D1 /
  ADR-0106 D3 (triggers belong to the pipeline) with one trigger kind. **Serves** ADR-0127 D1
  (the public example proves watches and events).

## Context

1. A `kind: watch` with a `source:` materialises as a schedule row whose workflow is the
   platform pipeline `dataset-readiness/watch-source` (`engine/triggers_grammar.py`,
   `WATCH_PIPELINE_REF`). That pipeline detects the change (`ingest/watch`), imports it through
   the governed `clean` flow (s3 / table), commits the marker, and proposes (`monitor/propose`)
   or starts (`monitor/dispatch`) the declaring workflow. Every leg but the last two is a
   platform operator or flow. On a public install the row fires a pipeline that does not exist.
2. A `dataset` source imports nothing: its bytes went through admission when the version was
   registered. Its detection is one pure comparison — the latest registered version against the
   marker on the row (`operator_ingest/_watch.py:probe_dataset`) — over the artifact store core
   already owns (`build_artifact_store().list_dataset_versions`).
3. #2605 let a pipeline leg `emits:` a domain topic and wait `trigger: on:<event>`. A step-level
   wait only holds a pipeline that is already running. ADR-0127 D1 says `ask-the-docs` *starts*
   on `profile-and-publish`'s `dataset.ready`; nothing can start a pipeline on a bus event today.
   The only bus-to-run path is `dispatch/bus_bridge.py`, which activates legacy skills.

## Decision

**D1 — A declared trigger starts the pipeline that declares it.** Every armed trigger kind ends
in the one launch door (`dispatch/start.py:launch_ref`) with the declaring pipeline as the
target, as `schedule` rows already do and as a pipeline's `webhook` now does (D3). A pipeline's watch is the exception only where
the platform's admission or proposal chain would be needed (D3): then it is named unarmed.

**D2 — The `dataset` probe is core's.** `probe_dataset` and its result type move to
`cairn/core/operator/watch_probe.py` (inside the operator contract root a pack may import); `operator_ingest._watch` imports them from there. One copy of
"did this registered dataset gain a version", read by core's fire path (D3) and by the platform's
`ingest/watch`.

**D3 — A pipeline's source watch is core's.** A FLOW's watch keeps ADR-0101 D4 unchanged (the
platform's `watch-source` row, which proposes or starts that flow). A PIPELINE's watch was never
armed (the platform's propose/dispatch chain starts flows, not `pipeline:` refs); now:

- a `source: {kind: dataset}` watch with `on_change: start` or `none` → a schedule row whose cron
  is `every:` and whose workflow is **the declaring pipeline**. The watch rides the row as the
  reserved input key `$watch` (`{source: dataset, dataset: <name>, on_change}`); its marker rides
  the existing `StoredSchedule.watch_state` blob (no column, no store). At fire time the scheduler
  (`dispatch/scheduler.py:fire_scheduled_workflow`) strips `$watch`, runs the D2 probe against the
  marker and:
  - unchanged → stamps `checked_at` and launches nothing;
  - changed, `on_change: start` → launches the declaring pipeline (the row's other inputs, which
    carry `dataset: dataset:<name>@latest` unless the declaration set `dataset`), then — only
    after the launch door accepted it — advances the marker. A refused launch keeps the marker,
    so the next tick tries again;
  - changed, `on_change: none` → advances the marker only.
- `s3`, `table`, or `on_change: propose` on a pipeline → not armed, reported in
  `MaterialiseResult.unarmed` with the reason (the S1 treatment, #1543) — never a silent cron.

A pipeline's `webhook` is armed too: it was held back because the receiver's run path took the
template door, but since #2115 the router starts through the one launch door
(`unattended_starter` → `launch_ref`), which takes a `pipeline:` ref.

**D4 — `kind: event`.** The trigger grammar gains a fifth kind:

```yaml
triggers:
  - kind: event
    topic: dataset.ready          # a domain topic; `run.*` is reserved
    inputs: {source: "$event.key"} # literal, or `$event.<path>` into the event payload
    enabled: false                 # materialised paused unless true, like every trigger
```

It materialises as a row in the EXISTING trigger store (`StoredTrigger`): spec `source: event`,
`ingress: {transport: bus, topic: <t>}`, `run: {template: <declaring pipeline>, input: …}` —
validated by the same spec loader, owned by the declaring pipeline (`declared_by`), paused /
resumed per workspace in Settings › Triggers, swept when the declaration is removed. The spec
loader learns the `bus` transport (`topic` instead of `path`/`method`).

**D5 — One bus subscriber fires event rows, on the leader only.** `dispatch/event_triggers.py`
(`EventTriggerBridge`, a sibling of `BusSkillBridge`) subscribes to the topics of every enabled
event row across workspaces, and on a message fires each matching row through `launch_ref` in the
row's workspace, inputs resolved from the payload with the receiver's own `$event.` resolver. It
runs under `Leadership.manage` like the scheduler, so N replicas launch once. A row added, paused
or removed re-syncs the subscription. `run.*` (the engine's lifecycle firehose) never fires a row.

**D6 — No self-loop.** A pipeline whose event trigger's topic is emitted by one of its own legs
is refused at parse: every run would start the next. Longer cycles across pipelines are not
detected; the README and the trigger docs say so.

**D7 — Delivery.** A message that arrives while no leader is subscribed is not replayed in this
record (the Postgres bus's replay cursor is per subscriber; giving the bridge a durable cursor is a
follow-up). On the in-process bus only a live leader in the same process sees an event.

## Consequences

- A public install proves `dataset` watches and event-started pipelines with core alone;
  `s3`/`table` watches and `propose` stay platform features and say so at materialisation.
- `ask-the-docs` can declare `kind: event, topic: dataset.ready` and still runs by hand.
- One more trigger kind in a public grammar (additive); one more transport in the receiver spec
  (additive); one new dispatch module; no new store, column, env knob or entry-point group.

## Verification

- Unit: the D3 selection (installed vs not; dataset start/none; s3/table/propose refused); the
  fire path (unchanged → no launch, changed → launch then marker, refused launch → marker kept);
  the D4 grammar and spec round-trip; D6 refusal.
- Integration: a real `PipelineBridge` + in-process bus: a published `dataset.ready` starts the
  declaring pipeline once; a paused row starts nothing; `run.*` starts nothing.
- The hello-cairn example declares both and the WP7 harness proves them in its cells.
</content>
</invoke>
