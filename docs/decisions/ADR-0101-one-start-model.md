# ADR-0101 — One start model: triggers on the workflow, inputs-only runs, one launch call, MCP legs

- **Status:** ACCEPTED (owner: "ok do it", 2026-09-03)
- **Date:** 2026-09-03
- **Epic:** #1972 (S1 #1973 · S2 #1974 · S3 #1975 · S4 #1976 · S5 #1977 · S6 #1978)
- **Supersedes nothing.** Extends ADR-0096 (YAML is the record), ADR-0068/0070
  (config scoping), ADR-0034 (HITL gates). Additive only, per the #1828 owner
  rule: flag-free, nothing deleted, every existing door keeps working.

## Context

The owner's verdict (2026-09-03): "right now it feels complicated, and no MCP
legs support." A survey of the console and the CLI shows why. The engine model
is sound and already Kestra-shaped; the *surface* exposes the engine's
taxonomy instead of a user's intent. To make a run happen a user meets six
doors, each with its own form and vocabulary:

| Door | What it asks today |
|---|---|
| Workflow › Run | before-you-run checks, capabilities, gates, inputs, saved inputs, config preset, environment, series, parent run, sample data, dry-run |
| Playground | workflow, environment, config preset, explain mode, judge sample |
| Settings › Triggers › receiver | source, path, method, signature header/scheme/secret/required, normalize fields, match filter, workflow, input mapping |
| Settings › Triggers › schedule | cron, workflow, inputs, environment, config preset, misfire policy |
| Settings › Triggers › watch | signal, comparator, threshold, current value, estimated cost, propose-run |
| Data › Import · Pipelines › Launch · `cairn run` | their own forms and their own launch calls |

Triggers live on a settings page away from the workflow they start. Workflows,
pipelines and the playground each have a separate launch call
(`runTemplate`, `POST /pipelines/launch`, the playground path). Ingestion is
three doors (import dialog, watch pipeline, schedule). Configuration layers
run › workflow › environment › default, and a governance-scoped `config_schema`
knob silently *discards* what a run passes (the #905 trap: ADR-0068/0070 make
such a field environment-owned). MCP exists as the `mcp/call` operator and `kind: mcp`
connections, used by three flows to publish to Notion — hand-written, no tool
discovery, no way to add an MCP step from the console, no risk class per tool.

### What comparable platforms do

| Platform | Pattern worth copying | Pattern to skip |
|---|---|---|
| **n8n** | every workflow starts with ONE trigger node; Manual / Schedule / Webhook / App-event are *kinds* of it. Pinned sample data for test runs. | free canvas, mutable JSON record, no governance |
| **Kestra** | `inputs:` typed in the flow → the Execute dialog asks only for those; `triggers:` block *in the flow file*; namespace KV for variables + secrets | its own control plane (rejected in ADR-0096) |
| **Temporal** | ONE client call starts anything (`start_workflow(name, args)`); schedules are first-class with pause / backfill / overlap policy | code-first authoring |
| **Langflow** | a playground tab on every flow; run-time "tweaks" override without editing; **MCP both ways**: add a server → pick a tool → it is a component; every flow exposed as an MCP tool | untyped prompt-glue, no gates |
| **Dagster** | sensors: a watch is a trigger kind that fires on new data | asset graph as the primary model |

The common thread: **a workflow owns how it starts**, a run asks only for its
declared inputs, and there is one verb to start anything.

## Decision

**D1 — Triggers belong to the workflow.** A flow declares how it starts in its
own front matter, Kestra-style:

```yaml
triggers:
  - kind: manual                      # always present; "Run now"
  - kind: schedule
    cron: "0 3 * * *"
    misfire: skip
  - kind: webhook
    source: pagerduty                 # a receiver preset (existing layer ①/③ spec)
    match: {event_type: [incident.triggered]}
  - kind: watch
    signal: dataset.rows              # existing monitor/propose semantics
    when: "> 1000"
    propose: true
inputs:                                # existing input_schema, unchanged
```

The console shows a **Triggers tab on the workflow page** listing these kinds
and editing them through a schema form (ADR-0096 D1: YAML is the record,
rendered one way). `Settings › Triggers` stays as the cross-workflow index.
The existing stores (`StoredTrigger`, `StoredSchedule`) become the
*materialisation* of the declared triggers for a workspace, not a second
source of truth. Nothing is removed: a receiver or schedule created in
Settings still works and shows up on its workflow's tab.

**D2 — A run asks for inputs only.** The run dialog shows the declared
inputs and a Run button. Environment, config preset, series, cost cap and
parent run move under an *Advanced* fold with defaults from **workspace
variables** (a small KV per workspace, Kestra's namespace variables), so a
normal run needs nothing else. Preflight and dry-run stay, one click away —
none of the surveyed platforms has them and they are cairn's edge. The builder
gains a lint: a per-run bound in a governance-scoped `config_schema` field is
flagged ("this value will be dropped at run time; declare it in
`input_schema`"), closing the #905 trap at authoring time.

**D3 — One launch call.** `POST /start` with `{ref, inputs, mode, …}` starts a
workflow, a pipeline or a sandbox run; `mode: sandbox | live` replaces the
playground as a separate product (the playground page remains, calling the
same route). `cairn run` mirrors it. The existing routes stay and delegate.

**D4 — Ingestion is a trigger kind.** `kind: watch` with a `source:` (a
registered dataset, an S3 prefix, a table through the ingestion pack) fires
when the source changes, registers a version through the existing governed
import, and optionally starts a workflow. Import-a-dataset becomes "import
once"; the watch pipeline and the schedule stop being separate doors.

**D5 — MCP legs, both directions.**
- *Consume.* An MCP connection gets tool discovery (`list_tools` exists in
  `cairn/services/mcp/client.py`). The builder catalog gains an **MCP tools**
  group; picking a tool emits an `mcp/call` step whose argument form is
  generated from the tool's own `inputSchema`. The risk class comes from a
  per-connection allowlist (`read` | `write`); a write tool gets a gate by
  default (ADR-0034), so "publish to Notion" needs no hand-written approval
  block.
- *Expose.* `cairn mcp serve` publishes every enabled workflow as an MCP tool
  (name = ref, input schema = `input_schema`, the call = `POST /start`), so an
  outside agent starts a *governed* run and gets the receipt back.
- MCP-sourced triggers (an MCP server as an event source) are explicitly
  deferred.

**D6 — Unchanged and protected.** The YAML record, server-side validation,
gates and receipts, the dry-run preflight, and the pack contract. No new
control plane, no new runtime, no operator authored from the console
(ADR-0096 D7).

## Verification (verify-upstream, 2026-09-03)

| Check | Result |
|---|---|
| A · LangGraph | not applicable — orchestration surface, not graph state |
| B · Temporal/Restate | Temporal *schedules* semantics (pause/backfill/overlap) inform D1; cairn's `StoredSchedule.misfire` already covers the essential; no engine change |
| C · Python ecosystem | `mcp` client already a dependency (`cairn/services/mcp`); `croniter`/APScheduler already drive schedules; nothing new |
| D · this codebase | **wire, don't build**: `StoredTrigger` + `StoredSchedule` stores and routers; receiver presets (`_TRIGGER_PRESETS`); `monitor/propose` watch semantics; packs already ship a `triggers/` dir (layer ①); `mcp/call` operator + `kind: mcp` connections + `mcp_server_spec` + `list_tools`; `GET /builder/catalog` palette; `POST /pipelines/launch`, `runTemplate`, the playground sandbox; input presets; config presets (ADR-0071); preflight (ADR-0072) |

**New code (build):** the `triggers:` front-matter key and its parser/validator;
the workflow Triggers tab + schema forms; workspace variables (store + route +
Advanced-fold defaults); `POST /start` façade; the `config_schema`-scope lint;
MCP tool discovery in the builder catalog + generated arg forms + risk
allowlist; `cairn mcp serve`.

**Not built:** a canvas, a second scheduler, a new trigger store, an MCP
gateway, a new runtime.

## Slices (epic #1972)

| Slice | Content | Depends on |
|---|---|---|
| S1 | `triggers:` in flow front matter → parsed, validated, materialised into the existing stores; Triggers tab on the workflow page (read + schema forms); Settings › Triggers becomes the index | — |
| S2 | Inputs-only run dialog + Advanced fold; workspace variables (store, route, console) | — |
| S3 | `POST /start` façade + `cairn run` alignment; playground = sandbox mode | S2 |
| S4 | Ingestion as `kind: watch` with a `source:` (dataset · S3 prefix · ingestion-pack table) | S1 |
| S5 | MCP consume: tool discovery → builder catalog group → generated `mcp/call` step + risk allowlist + default gate on writes | — |
| S6 | MCP expose: `cairn mcp serve` (+ the config-scope lint, small, rides here) | S3 |
| S7 | D1–D3 for **pipelines**: `triggers:` on `PipelineDef` with `on:` as its legacy spelling, materialised by S1's code path; a Triggers tab on the pipeline's own page; inputs-only launch with S2's Advanced fold | S1 ∥ S2 |

Order: S1 ∥ S2 ∥ S5 → S3 → S4 ∥ S6, with S7 after S1 and S2. Each slice ships
behind nothing: every change is additive and the old doors keep working until
the owner retires them in a later ADR.

**S7's two decisions worth recording** (owner ask 2026-09-03, "implement the
same for pipelines too"; issue #1987):

- **`on:` and `triggers:` are refused together** rather than ranked. They are
  two spellings of one grammar, so a precedence rule would resolve the most
  likely mistake — converting an `on:` block and forgetting to delete it —
  either by silently dropping a declared trigger or by arming the same cron
  twice. `parse_pipeline_def` names it instead. No shipped pipeline can hit
  this: `triggers:` did not exist before this slice.
- **`on:` keeps the legacy defaults it has today**: the `pipeline--…` store id
  from #1159, and `enabled: true` on first materialisation. A `triggers:`
  schedule gets the S1 default of PAUSED (#1568). Re-keying would leave an
  orphan firing beside its replacement or reset an operator's pause; flipping
  `enabled` would silently stop a nightly that runs today. The difference is
  the second reason to write `triggers:`.

## Consequences

- A first-time user sees one page per workflow that answers "how does this
  run, and when": the largest single simplification available without touching
  the engine.
- MCP becomes a real leg with a governance story (read vs write → gate), which
  no surveyed platform offers.
- Two records for triggers exist during the transition (front matter and the
  stores); the stores are the materialisation and the front matter wins on
  conflict. The transition ends when Settings › Triggers only lists.
- Workspace variables add one small store (same shape as input presets).

## Alternatives rejected

- **Adopt n8n/Langflow's canvas** — rejected again (ADR-0096): no governance
  model, mutable record.
- **Run everything through Temporal schedules** — the misfire policy already
  covers what matters; a second scheduler adds an operational dependency to
  the light profile that is explicitly in-process only (#1828).
- **An MCP gateway in front of the engine** — a second control plane; the
  operator + connection + allowlist is enough and keeps the risk class where
  the gate logic already lives.
