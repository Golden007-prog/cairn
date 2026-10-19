# ADR-0076 — Loop-aware core: govern the loop, not just the run

Status: Proposed (2026-07-17)
Relates to / builds on: ADR-0047 (run provenance / receipts — the `Run` model this
extends), ADR-0069 (pipeline bridge — `bundle_pipeline_id`/`step_id`, the *formal*
lineage this generalizes to *ad-hoc* lineage), ADR-0071 (config presets — provenance
precedent), ADR-0072/0073 (preflight verdict + Playground — the `--dry-run`/`--mock`
plumbing `cairn gate` reuses), ADR-0059/#244 (pilot/experiment ledger — the grouping
`series` extends), ADR-0060/#480 + `eval/continuous.py` (durable-bus reactor — the
substrate watch-triggers ride), ADR-0010/0012 (notification-only + propose-not-apply —
the rule watch-triggers and `dataset-fix` obey). Companion analysis:
`design/cairn-vs-standard-practice-gap-analysis.md`.

## Context — the one structural gap

cairn governs a **run**: `trigger → compile → guarded run → gated actions → receipt`.
That is the right unit for the *expensive or risky* steps of ML/ops work — GPU spend,
a promotion, a prod mutation. But the work practitioners actually do is a **loop**:

- Fine-tuning is an *experiment loop* — many runs, compared, data fixed, repeat.
  ([ThirstySprout](https://www.thirstysprout.com/post/fine-tuning-llm))
- Dataset prep is a *fix-and-recheck loop* — eval → repair → eval.
  ([Meta-Intelligence](https://www.meta-intelligence.tech/en/insight-finetuning-data))
- Incident RCA is a *live evolving loop* — evidence arrives minute-by-minute.
  ([SRE School](https://sreschool.com/blog/root-cause-analysis-rca/))

The risk if cairn stays run-shaped: practitioners iterate *outside* cairn and pass only
the final artifact through, reducing cairn to a compliance checkpoint instead of a daily
tool. The industry's mature pattern is event-driven MLOps with eval gates enforced by the
orchestrator ([Google Cloud MLOps](https://docs.cloud.google.com/architecture/mlops-continuous-delivery-and-automation-pipelines-in-machine-learning)) —
which is cairn's native shape *plus* cost caps + audit, but only if cairn is loop-aware.

## Decision

Make the **engine loop-literate while keeping every command one-shot.** cairn never
*generates* the next iteration (no auto-sweeps deciding hyperparameters, no unattended
agent looping over training). The human (or their tools) drives the loop; cairn makes each
turn cheap, lineage-linked, budgeted at the series level, and gated at the moments that
matter. This is delivered as **generic cairn-core extensions**, not per-workflow features.

Five changes, each reusing an existing seam (see the `/verify-upstream` report in #EPIC):

### D1 — Run lineage (`parent_run`) — the keystone

Add a nullable `parent_run: str = ""` to `Run` (`core/storage/protocols.py`) and a
`--from <run-id>` option to `cairn run`. `dataset-eval → dataset-fix → finetune → promote`
becomes a **provenance chain, not orphans**; `cairn log --follow-chain` renders it.

- **Fact:** `Run` today has `bundle_pipeline_id`/`step_id` (formal pipeline membership,
  ADR-0069) but **no ad-hoc parent pointer**, and `cairn run` has no `--from`. So a
  hand-driven loop is unlinked.
- **Correct practice:** content-addressed reproducibility + lineage is the audit
  differentiator trackers don't have; the *decision* must carry the loop's history.
- **Seam:** `storage/migrations.add_missing_columns` (`ADD COLUMN IF NOT EXISTS` +
  backfill — the exact mechanism used for `workspace_id` and the eval-ledger columns),
  symmetric sqlite/postgres. **This is the enabling change D3/D4/D5 all depend on.**

### D2 — `cairn gate` — the CI/CD surface

A read-only CLI verb that runs a pack, emits a machine-readable verdict (JSON + non-zero
exit on FAIL), and (in CI) posts cited findings as a PR comment; plus a published
composite GitHub Action.

- **Fact:** no `gate` verb exists; but `--dry-run` and `--mock` are already real options on
  `cairn run` (`cli/commands/run.py`). The gap is *packaging*, not engine.
- **Correct practice:** teams enforce eval gates *inside* GitHub Actions/GitLab on every PR
  (Google MLOps level 2). cairn has the gate primitive but no presence where gates run.
- **Seam:** wrap `run` + preflight (ADR-0072); ship `cairndev/gate-action@v1`. Independent
  of D1 — smallest, highest adoption-per-effort.

### D3 — `dataset-fix` — repair path after a failed gate

A template whose **input is a prior run's findings** (`--from run-id`): propose fixes
deterministically, apply only through the approval gate, record provenance both ways.

- **Fact:** the repair operators **already ship** — `anonymize` (PII redact+write),
  `dedup`, `decontam`, `curate` (AlpaGasus/DEITA LLM-judge selection). They take a file
  `path`, not a run's findings. The missing piece is the *run-output-as-input seam* — i.e.
  D1's `parent_run`.
- **Correct practice:** prep is where ~80% of the time goes; a gate without a repair path
  captures the last 5%. **But** aggressive global fuzzy dedup can *hurt* models by
  destroying diversity ([MixtureVitae, arXiv:2509.25531](https://arxiv.org/pdf/2509.25531)) —
  so `dataset-fix` **proposes, and applies only through the gate** (a human approves the
  destructive write). Never auto-scrub.
- **Seam:** existing operators + `actions/executor.py`/`durable.py` gated-write saga +
  `slack_approver`. Much smaller than a from-scratch build because the operators exist.

### D4 — Watch-mode triggers — the data-shaped trigger (generic)

A **new core trigger type**: a scheduled skill polls a detector capability and, past
threshold, **files a proposed run for approval** — never auto-spends.

```
⏸ drift on chatbot-intents (PSI 0.31 > 0.2) · proposed: finetune retrain-oct · est $80 · approve?
```

- **Fact:** `grep drift` finds no detector. But the substrate exists: `TriggerProvider` ABC
  on entry-point `cairn.trigger_providers` (`GenericYamlProvider` is the pattern),
  `SkillScheduler` (apscheduler) already fires scheduled skills, and `eval/continuous.py`
  already reacts on the durable `PostgresBus`. Only the detector + a `WatchTriggerProvider`
  are new.
- **Correct practice:** data-driven retrain triggers (drift / data-accumulation) are the
  industry's highest-value trigger ([apxml](https://apxml.com/courses/mlops-for-large-models-llmops/chapter-6-advanced-llmops-systems-workflows/automating-llm-retraining));
  the on-brand form is **human-gated** (propose → approve), preserving "cairn never spends
  without approval" (ADR-0010/0012).
- **Detector placement (decided):** the **generic** `signal/threshold` reader (zero-dep;
  reads a numeric metric from a pluggable source — file/HTTP/SQL — vs a threshold) lives in
  **`operators/builtins/`** next to `data_profile`/`redact_regex`. **Vendor** detectors
  (Evidently, Arize, whylogs) are **`official` packs with `[extra]`** implementing the same
  capability, mirroring `s3`/`training`. Base install can watch with no new deps; vendors
  plug into the same seam. The *trigger* is the core extension; the operator is the plugin.

### D5 — Series — named run sequence with a shared budget

A lightweight grouping primitive (**not** a sweep scheduler): `cairn series start/compare`,
`cairn run --series <id>`, `cairn promote best(<id>)`. The governance win is unique: **the
budget caps the *series*, not the run** — "you may spend $300 exploring."

- **Fact:** the cost gate's thresholds are **per-run/per-deployment** (`compute/cost_gate.py`);
  no series budget. The pilot/experiment ledger already groups `experiment → runs → scorecards`
  (`core/pilot_ledger.py`, `cairn pilot experiment`) — the grouping exists, but it's
  eval-scored, not cost-budgeted, and has no cost+score compare table.
- **Correct practice:** the unit of fine-tuning is the experiment series; no tracker
  (W&B/MLflow) enforces a *spend cap* on exploration.
- **Seam:** extend the pilot grouping + reuse the cost-scope aggregation in
  `dispatcher`/`gateway`/`pilot`; a compare view over run metadata. Deliberately not a sweep
  scheduler — the user/their tool launches each run; cairn governs and compares.

### D6 (folded) — RCA timeline + side-by-side eval

Lower-leverage, real: emit the reconstructed **chronology** as a first-class RCA output
section (nearly free — evidence-gather already timestamps everything it collects — this is
*rendering*, not new collection); and a paired base-vs-tuned **output diff** review surface
for the qualitative half of fine-tune eval ([ThirstySprout](https://www.thirstysprout.com/post/fine-tuning-llm)).
Tracked, sequenced last.

## Scope corrections (claims struck as stale)

Two "gaps" in the companion analysis are disproven by the code and are **not** in scope:

- **"train.run is stubbed / GPU unverified."** `operator_training/train_entry.py` is a real
  Unsloth QLoRA SFT entrypoint (`trl`/`unsloth`/`peft`); GPU epic proven live on prod. Honest
  residual = first-run onboarding UX, not "it's a stub."
- **"Cascade rides InProcessBus / single-node / outbox unbuilt."** `core/bus/postgres.py`
  exists; `OBS_BUS=postgres` is a durable bus with replay-on-arm; `eval/continuous.py`
  consumes it cross-process.

## Tool selection (build-vs-buy, licenses verified via each repo's LICENSE)

Every "buy" is Apache-2.0/MIT, installs as a **pip-only optional `[extra]`** (cairn's
existing `[pii]`/`[minhash]` pattern), and needs **no new runtime**:

| D | Buy (extra) | License | Build (cairn-core) |
| --- | --- | --- | --- |
| D1 | OpenLineage client (emit) | Apache-2.0 | `parent_run` pointer |
| D2 | — | — | `cairn gate` verb + Action |
| D3 | cleanlab, datatrove (NeMo Curator opt-in, GPU) | Apache-2.0 | run-as-input wiring |
| D4 | Evidently (default), NannyML (opt-in) | Apache-2.0 | `WatchTriggerProvider` + signal reader |
| D5 | mlflow / W&B (already wired), Optuna | Apache-2.0 / MIT | series-scoped budget |
| D6 | deepeval `arena_g_eval` + lm-eval (both already pinned) | Apache-2.0 / MIT | RCA timeline + A/B render |

**Rejected on license/runtime grounds:**
- **Alibi Detect** (D4) — **Business Source License 1.1**, not OSS; forbids commercial
  product inclusion. Evidently + NannyML cover the need.
- **promptfoo** (D6) — MIT but **Node.js runtime**; cairn stays pure-Python.
  `deepeval.arena_g_eval` (already pinned) is the pairwise judge.
- **Arize Phoenix** (D6) — Elastic License v2 (source-available) + needs a server.

**Infra-heavy → opt-in externals only, never bundled/required:** NeMo Curator (GPU+Ray),
Marquez (Java server + Postgres — cairn only emits OpenLineage; Marquez is the user's
optional consumer).

## The line that must not move

cairn never generates the next iteration itself. No auto-sweeps, no unattended training
loops. Loop-awareness *strengthens* the determinism brand: govern the loop, not just the run.

## Sequencing

**D1 (lineage) → D2 (gate) → D3 (dataset-fix) → D4 (watch/drift) → D5 (series) → D6 (timeline).**
Rationale: D1 is the shared keystone (D3/D5 need it); D2 is independent and cheap so it lands
in parallel; D3 reuses D1's run-as-input seam; D4/D5 are additive; D6 is nearly free rendering.

## Consequences

- Run store gains one nullable column (backward-compatible migration).
- One new CLI verb (`gate`) + one grouping verb family (`series`).
- One new builtin detector operator + a documented capability for vendor adapters.
- One new `WatchTriggerProvider`; no new trigger *mechanism*.
- No new durability tier, no new bus, no new base deps, no new dataset operators.
