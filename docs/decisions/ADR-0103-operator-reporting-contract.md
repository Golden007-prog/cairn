# ADR-0103 — The operator reporting contract: every operator declares what it reports, the base records what it ran with

- **Status:** ACCEPTED (owner, 2026-09-05: "we also need a design system that operator should expose metric, config etc otherwise in future we will make the same mistake again, or base operator should have method")
- **Date:** 2026-09-05
- **Extends:** ADR-0026/0020 (the pack contract), ADR-0082 D8a (stage events for flow engines, #1076), ADR-0068/0070 (config scoping), CLAUDE.md rules 7 and 8. Referenced by ADR-0102 D7.
- **Epic:** training-experience epic #2019, slice T16; foundation for T6 (#2025) and T7 (#2026)

## Context

ADR-0102 D7 makes the console's Activity page and Metrics tab generic: they render whatever each operator declares. That only holds if declaring is impossible to skip. Cairn has been here before: `infer/batch` shipped with `risk = external_write` in the manifest and `read_only` on the class through five green test runs, because the property lived in two places and nothing compared them until the pack was installed (rule 7). Reporting must not repeat that.

What exists today in `cairn/core/operator/protocol.py`: `Operation` carries `name`, `risk`, `executors`, `requires_approval`, the `supports_*` flags and the MCP fields, and `manifest_sync` compares each against the pack manifest. `OperationContext` carries an untyped `metrics: MetricsLogger` (`log(key, value)`, a null object by default), a `cost` ledger, an `artifacts` store, `emit_heartbeat` and `check_cancelled`. `stage_events.py` turns the flow engines' poll metrics (`stage`, `stages_done`, `stages_total`, `elapsed_s`) into `stage` run events with the rule that it never raises and never blocks. So the transport, the gate and the context all exist; what is missing is a declaration, a typed reporter, and the base recording effective inputs.

## Decision

**D1 — `reports` is a required class attribute on `Operation`, beside `risk`.**

```python
class Operation:
    name = "train/local"
    risk = "read_only"
    reports = ReportSpec(
        stages=["download", "tokenise", "train", "eval", "export"],
        series=[Series("loss", "nats"), Series("eval_loss", "nats"),
                Series("learning_rate", ""), Series("grad_norm", ""),
                Series("tokens_per_s", "tok/s")],
        scalars=["final_loss", "best_eval_loss", "tokens_seen", "steps"],
    )
```

An operator with nothing to report says so: `reports = ReportSpec.NONE`. A missing attribute fails the manifest-sync gate exactly as a missing `risk` does. The manifest mirrors it as `[operator."train/local".reports]` with `stages`, `series` (name, unit), `scalars`; `manifest_sync` compares class and manifest and fails on any difference. `governance_drift` treats a changed declaration as drift.

**D2 — The context reporter is typed and validated against the declaration.** `OperationContext.report` replaces the untyped `metrics.log` for new code (the old logger stays for one release, then goes):

- `ctx.report.stage(name, percent=None, eta_s=None)` → a `progress` run event;
- `ctx.report.series(step, **values)` → a `metrics` run event;
- `ctx.report.scalars(**values)` → a `scalars` run event at the end;
- `ctx.report.checkpoint(ref, step, metric=None)`, `ctx.report.divergence(kind, step, value)`.

An undeclared stage, series or scalar name **raises in tests and under `cairn validate`**, and in a running job is **dropped with one warning per name, never raised**, mirroring the stage-events contract: a reporting bug must not fail a paid job. Every event carries `(run_id, seq)`; the reporter spools locally and replays on reconnect (ADR-0102 D4). Flow engines keep their existing poll-metrics path; `stage_events.py` becomes one producer of the same `progress` event.

**D3 — The base runtime records the effective inputs and config of every step before calling `run`.** In `executor.py`, after config scoping and defaults resolve, the runtime writes one `parameters` block on the step's run: the effective inputs, the effective config with each field's scope (run · workflow · environment · default), and the resolved connection and compute references. No operator does this itself; none can forget it. This block is what the Metrics tab's Parameters section, preview, import/export, clone-from-run and diff read, for any operator. For a training step it is the hyperparameters.

**D4 — The design record and the scaffold carry the declaration.** `docs/templates/operator-pack-DESIGN.md` §3 "Operators" gains columns for stages, series and scalars, and `test_pack_design_docs.py` fails a record whose §3 does not mention every declared series and scalar name (the same rule it already applies to operator names). `cairn init operator` emits `reports = ReportSpec.NONE`, the manifest table and the DESIGN row, so a new operator starts compliant and must consciously delete the declaration to lose it. The operator conformance suite (`cairn/core/operator/testing.py`) gains a case: every declared series name appears in at least one emitted event during the operator's mock run, so a declaration cannot be decorative.

**D5 — Backfill.** Every shipped operator gets `reports` in one PR per pack. Operators that do nothing long-running declare `NONE`; `dataset_checks`, `ingest`, `infer/batch`, `redteam`, `serve/*`, `train/*`, `compress/*` declare real stages. The backfill set is a frozenset in the gate that may only shrink, as `BACKFILL` in the design-docs gate does (#1997).

## Verification (verify-upstream)

| Check | Result |
|---|---|
| A · LangGraph | not applicable |
| B · workflow engine | the run-event store and SSE bridge carry the events; the `loop:` leg and gates are unchanged |
| C · Python ecosystem | nothing new; pydantic models for `ReportSpec` |
| D · this codebase | **extend, do not duplicate**: `Operation` + `manifest_sync` (add one attribute and one comparison), `OperationContext` (add the reporter beside `emit_heartbeat`), `stage_events.py` (generalise from `stage` to `progress`), `executor.py` (record parameters), `testing.py` conformance, `test_pack_design_docs.py`, `cairn init` |

**Build:** `ReportSpec` + `Series` models; the `reports` attribute and its manifest table; the typed reporter with spool and validation; the `parameters` block in the executor; the three event types; the gate extensions; the scaffold; the backfill frozenset. **Not built:** a second metrics API, a training-specific reporter, or any change to what the console renders beyond reading the declaration.

## Consequences

- The Activity page and Metrics tab (ADR-0102 D7) become impossible to starve: an operator either declares and emits, or fails the gate.
- Every step's effective configuration is on the run record without any operator writing a line, which retires the class of bug where a spec is a lie after defaults resolve.
- One-time churn across ~35 packs, bounded by the shrinking backfill set; each pack's DESIGN.md gains a few table columns.
- The flow engines' stage contract from ADR-0082 D8a is folded into the general one rather than remaining a special case.

## Alternatives rejected

- **Mandatory only for long-running operators** — a short operator that later grows long slips through; the property must be explicit everywhere, as `risk` is.
- **Optional with a default NONE** — the convention that failed once already.
- **Inferring the declaration from emitted events** — no units, no ordering, no lint for a misspelt metric, and a layout that shifts as events arrive.
- **A metrics API separate from the run-event stream** — a second transport to keep reliable; the store is already the log.
