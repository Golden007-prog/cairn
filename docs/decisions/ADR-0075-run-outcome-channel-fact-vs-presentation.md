# ADR-0075 — The run **outcome channel**: fact-vs-presentation boundary for the pilot / result surface

Status: Proposed — **under discussion** (2026-07-15)
Relates to / builds on: ADR-0073 (the Playground — where the confusing result
first surfaced), ADR-0074 (input `data/profile`), ADR-0070/0068 (config scoping
— the coercion gap named below), ADR-0047 (run provenance), ADR-0020/0026 (pack
contract; **packs are DATA**, operator packs own their impl), #248/#242 (the
pilot report + captured cost), #865/#866 (the Playground fixes that exposed this),
#822 (Playground epic). Companion: `cairn/core/pilot.py` (`build_pilot_report`,
`_outcome_from_payloads`), `design/cairn-playground-v2.html`.

## Context — why the verdict never reaches the UI

Testing the Playground on `dataset-readiness/check` surfaced a correctness bug
that looks like a UI bug but isn't: **the verdict hero shows green "Clean" even
when the run actually fails** (`pii_rows: 1`, readiness `verdict: fail`).

Root cause, traced end to end:

1. The pilot report already has a first-class **outcome channel** —
   `_outcome_from_payloads` reads `result_payloads[*].output.outcome` (a list of
   `{name, value, pass, target}` metrics) into `captured.outcome`.
2. **No operator emits `output.outcome`.** `grep 'outcome' cairn/operators/**`
   returns nothing. So `captured.outcome` is *always* empty, for every workflow.
3. The verdict the operators DO compute lives only in
   `result_payloads[0].output.findings.verdict` / the synthesized markdown — a
   place nothing on any surface reads for a headline pass/fail.

So the fix is **not** missing logic in core, and **not** the console parsing a
report. It is: operators aren't filling a channel that already exists.

A second, independent finding from the same investigation (kept here so the
boundary is on record, fixed separately): the config store accepted a
`target_trainers` **CSV string** for a `list[str]` knob (`PUT /config/values`
did no schema coercion), which cascaded `dataset/format → dataset/gate →
audit/store` into binding failures — so the gate never even ran. That is a
config-layer robustness gap (ADR-0070), not a report gap.

## Decision — the boundary: **facts are produced by operators; presentation is the console**

`core/` is not one layer. We draw the line by *fact vs presentation*, not by
directory:

| Layer | What it is | Rule |
|---|---|---|
| `core/{engine,runtime,dispatch,policy,storage,actions,pack,eval}` | the governed engine — how runs execute / gate / persist | **Off-limits for UI work.** ADR + real reason only. |
| `core/pilot.py`, parts of `core/triggers/server.py` | report **assembly**, shared by ALL surfaces (CLI/TUI/web/HTTP/Slack) | change only to *faithfully surface a fact the run already produced* — never UI heuristics |
| `core/console/api.py` + console-only endpoints | presentation / aggregation for the **web** surface | safe home for console-only logic |
| `operators/*` | where **facts are produced** | run-level verdicts/metrics belong here, published as `output.outcome` |
| `cairn/web` | pure rendering | everything visual |

Heuristic: *"what the run truly did"* → an operator publishes it as a fact.
*"how to show it"* → console/web. The run engine is not touched for any of this.

### 1. The outcome channel is the contract (operator-owned, deterministic)

An operator that reaches a domain conclusion publishes it as
`output.outcome: list[OutcomeMetric]`, where an `OutcomeMetric` is:

```
{ "name": str,            # human label, e.g. "readiness", "pii rows"
  "value": str | number,  # "fail" | 1 | 66.7
  "pass": bool,           # did this metric pass its bar
  "target"?: str | number # optional bar, e.g. "pass" | 0 | 95 }
```

Rules:
- **Deterministic only.** The outcome is computed by the deterministic operator
  (for dataset-readiness, `dataset/gate`), never by an LLM narration step (the
  §4 determinism rule: the engine decides go/no-go, the LLM only narrates).
- It surfaces via the **existing** `_outcome_from_payloads` seam — bind the
  operator's `outcome` to `$state.outcome` so it lands at
  `result_payloads[0].output.outcome`. **No change to `core/pilot.py` needed.**
- Every surface (CLI/web/pilot page/Playground) reads the same channel. One fact,
  many renderers.

For this ADR we keep `OutcomeMetric` a **documented dict convention**, not a new
shared core type — operators emit plain dicts. If ≥2 operators adopt it we may
promote a shared model into the operator SDK (a small, deliberate core change),
but not before.

### 2. Two verdicts, kept distinct — never conflated

There are two legitimately different "verdicts" on a run; the confusion came from
treating them as one:

- **Scorecard verdict** (`pilot.verdict`: `success | review | pending review`) —
  "was this *run* good quality", a 7-dimension review incl. human dims. Owned by
  `build_pilot_report`. **Unchanged.**
- **Domain verdict** (the workflow's outcome — the gate's `pass/warn/fail`) —
  "did the *dataset* pass readiness". Owned by the operator, published as an
  outcome.

The Playground hero and the pilot "result" reflect the **domain verdict**
(the outcome), not the scorecard. Rendering reads outcomes; it does not overwrite
`pilot.verdict`.

### 3. Where each rework item lands (near-zero core)

| Rework item | Home | Core touched? |
|---|---|---|
| Surface the verdict (hero flips honestly) | operator emits `outcome` (`dataset_checks`) + web reads it for `blocked`/tiles | **no** |
| Result-vs-report clarity, output/trace rethink | `cairn/web` + `core/console/api.py` | no |
| LLM eval configured + working | env/preset wiring (`eval/judge.enabled`, `playground-eval` route) | no |
| Config CSV→list coercion | console `PUT /config/values` handler first (ADR-0070 seam); lift to `workflow_config_store` only if the CLI needs it | console-layer only |

## Consequences

- The main correctness fix is an **operator-pack change** + a small **web**
  change. The run engine, storage, dispatch, and policy are untouched — which is
  exactly the caution this ADR exists to encode.
- Any future workflow gets an honest headline verdict for free by emitting
  `output.outcome` — the Playground/pilot/CLI need no per-workflow code.
- `pilot.verdict` (scorecard) and the domain outcome stay separate concepts; the
  UI stops implying "Clean" from an unreviewed scorecard.

## Not doing (now)

- No shared `OutcomeMetric` model in core (dict convention until multiple
  adopters).
- No overwrite of the scorecard verdict from the domain outcome.
- The config-coercion hardening and the eval enablement ship as their own slices
  under this boundary; this ADR records where they go, not their detail.
