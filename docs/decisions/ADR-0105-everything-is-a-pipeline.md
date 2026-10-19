# ADR-0105 — Everything you start is a pipeline: one record, one door, one console noun

- **Status:** ACCEPTED (owner: "yes option 1 is good", 2026-09-08)
- **Date:** 2026-09-08
- **Epic:** #2111 (S0 #2112 · S1 #2113 · S2 #2114 · S3 #2115)
- **Executes** ADR-0084 D9 (console noun is Pipelines), which was decided on
  2026-07-28 and never built. **Amends** ADR-0101 D1 (triggers belong to the
  workflow → to the pipeline; flow-authored triggers stay valid). Keeps
  ADR-0084 D0/D2/D4 and ADR-0101 D2/D3 unchanged. Additive: every existing
  route keeps working and delegates, per the #1828 owner rule.

## Context

The owner's verdict (2026-09-08), looking at the console on the #2109 branch:
"we still have pipeline & flows as separate entities with different run
mechanism and UI."

Verified in code the same day, the split is real at three layers:

| Layer | Flow (standalone) | Pipeline |
|---|---|---|
| Record | `Run` in the run store | `PipelineRecord` in the pipeline store (`status`, `step_state`, `cursor`) — a second state machine, in-process in `PipelineBridge` and durable in `PipelineWorkflow` (Temporal) |
| Start | `POST /start` → `RunDispatcher.run_template` | `POST /start` → `launch_pipeline`; the console still calls `/pipelines/launch` from the pipeline dialog, Datasets and the watch trigger |
| Console | Flows list (primary nav), flow detail with Run · Triggers · Runs tabs, `RunDetailView` | Pipelines list (flag-gated under "More"), `PipelineDefView`, `PipelineDetailView` with its own tabs and its own Triggers |

Each pipeline leg is already its own governed `Run` linked by
`bundle_pipeline_id` (ADR-0084 D0/D4). So a standalone flow run and a leg are
the same record; the pipeline is the extra thing, and it only exists when a
user started a pipeline.

ADR-0084 D9 already decided the console direction — "the Workflows surface
becomes Pipelines; flows demote to components, visible in the Library and
expandable inside the pipeline detail; Runs view groups legs by pipeline id."
The nested graph in #2109 is the "expandable inside the pipeline detail" half
of that. The rename in the same PR (Workflows → Flows as the PRIMARY noun) went
the other way; this ADR reverses it (S0) before it reaches prod.

## Decision

**D1 — Everything you start is a pipeline.** A bare flow ref started through
`POST /start` or `cairn run` becomes a synthesized ONE-LEG `PipelinePlan` (leg
id = the flow's name; ref `flow:<pack>/<name>` for data/ml, the bare ref for
agentic — the two leg shapes `PipelineBridge._submit_step` already runs) and
is launched through `launch_pipeline`. `mode: sandbox` rides on it. The bare
run routes stay and delegate.

**D2 — A run is a leg.** `Run` is unchanged as the governed unit: admission,
budget, policy, approvers, audit — per leg (ADR-0084 D4). Every `Run` now
carries `bundle_pipeline_id`; `template_ref` keeps its ADR-0084 D0 meaning, so
insights, eval scoring and the Runs scoping need no special case. The pipeline
row gets a rollup (legs done/total, terminal status, waiting-on, cost).

**D3 — Console noun is Pipelines (ADR-0084 D9, executed).** Sidebar: Pipelines
first; Flows under "More" as the component library. ONE list — Runs shows
pipelines as rows with legs beneath. ONE launch dialog on `/start`. ONE detail
page — `PipelineDetailView` with the nested graph (#2109); clicking a leg opens
today's run detail as the leg view. `/runs/:id` stays as a deep link and
redirects into its pipeline. The separate Pipelines page becomes the
definitions library (what you can run, with its plan graph).

**D4 — Triggers belong to the pipeline** (amends ADR-0101 D1). New triggers are
authored on the pipeline page (ADR-0101 S7 already put a Triggers tab there).
Flow-authored `triggers:` front matter stays valid and fires the flow's
one-leg pipeline; the Triggers, Run and Runs tabs leave the flow page.

**D5 — The `pipelines` feature flag is a no-op.** The pipeline is the
substrate, so a profile cannot turn it off. The key stays accepted (old env
files validate), the route gates go, the light preset stops listing it.

## Verification (verify-upstream, 2026-09-08)

- Check D (this codebase): `POST /start` façade exists (ADR-0101 S3, #1975);
  `PipelineBridge` already runs agentic legs via `run_template` and `flow:`
  legs as their own runs; `PipelineRecord` + `PipelineWorkflow` exist;
  `RunsView` already scopes by `?pipeline=`; `PipelineGraph` (#2109) draws the
  nested plan. **Nothing new is built at the engine's core** — S1 is a plan
  synthesizer in front of an existing launcher.
- Checks A/B/C: not applicable (no new graph, durability or library concept).

## Slices (epic #2111)

| Slice | Content | Depends on |
|---|---|---|
| S0 #2112 | Console stops contradicting D9: Pipelines primary, Flows under More; every console launch through `/start` | #2109 |
| S1 #2113 | Engine: bare flow ref → one-leg pipeline in `/start` + `cairn run`; pipeline rollup; `pipelines` flag no-op | — |
| S2 #2114 | Console: one Runs list, one launch dialog, one detail page (nested graph + leg view), redirects | S0, S1 |
| S3 #2115 | Flows demote: flow page loses Run/Triggers/Runs; triggers on the pipeline; flow triggers fire a one-leg pipeline; CLI + docs | S1, S2 |

Order: S0 now, before #2109 ships to prod → S1 → S2 → S3.

## Consequences

- One vocabulary end to end: **Pipeline → Leg → Stage / Node**. A leg is a run.
- One more `PipelineRecord` (and, on the durable tier, one `PipelineWorkflow`)
  per standalone start. S1 measures start latency on the prod compose.
- Approvals, the topbar badge and Insights read pipeline rows where they read
  runs today; the audit trail stays per leg.
- The light profile (#1828) keeps working: a one-leg pipeline of an LLM-step
  flow is exactly what it ran before, under a different record.

## Alternatives rejected

- **Unify the surface only** (one list/detail/dialog over two record kinds,
  engine untouched). Cheaper, but the two state machines and the duplicate
  trigger surfaces remain; the confusion returns the moment someone reads a
  run record or a trigger. Rejected by the owner in favour of D1.
- **Fold pipelines into flows.** Contradicts ADR-0084 D4 (the pipeline is the
  only orchestrator) and would put orchestration back into the flow body.
- **Keep Flows as the primary noun** (the #2109 rename). Contradicts D9 and
  hides the thing a user actually runs behind its components.
