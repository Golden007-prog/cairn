# ADR-0107: Outcome-first console — from a capability surface to a guided one

- **Status**: PROPOSED
- **Date**: 2026-09-09
- **Epic**: #2144 (slices #2145–#2151)
- **Amended**: 2026-09-10 — D6/D7/D9 generalised beyond training, D10 added,
  then four factual errors corrected in D5, D9 and D10 (each marked in place);
  then D3 amended after the owner saw the prototype and the shipped slices side
  by side — the guided *launch* ships (#2168), the guided *run* does not;
  the data-source row corrected (an upload path exists), sequencing rewritten
  after ADR-0106 S1/S2 merged. Design green-lit from the `proto/2144-guided-run`
  prototype.
- **Relates to**: ADR-0106/#2134 (only declared pipelines run), ADR-0100/#1838
  (workflow suites), ADR-0102 D2/#2028 (the config recommender), ADR-0105/#2111
  (everything is a pipeline), #2019 (training experience), #2154 (config is
  flow-scoped but ADR-0106 D4 says pipeline-scoped)

## Context

cairn can execute the whole improvement loop. A user cannot find their way into
it. A 2026-09-09 review of an external 7-stage fine-tuning design (goal → data →
prepare → baseline → fine-tune → evaluate → deploy), checked stage by stage
against the committed tree, found that roughly 70% of the machinery already
exists as operators — and that the missing part is not machinery at all.

**What the console offers today** (verified, 2026-09-09):

- A **setup** checklist — `WorkflowsView.tsx:57`, "Get your first governed run
  0/4": try the playground, see the receipt, connect a provider, enable a flow.
  It onboards the product, not a task.
- Two **tours** — a 5-stop walk (workflow → sandbox → run → receipt → promote)
  and a 3-step one in the Playground. Both teach concepts; neither does work.
- A **launch dialog** — `PipelineLaunchDialog.tsx` (559 lines): preflight,
  per-leg config knobs, base-model fit, cost preview, and the Suggested-config
  cards (#2028) that already say what each config sets and why. Good, and one
  screen rather than stages.
- A catalog filtered by **KIND** (flow / pipeline / bundle) — our taxonomy, not
  the user's.

**What is absent.** Grepping `PipelinesView`, `WorkflowsView` and `CatalogTab`
for `goal|achieve|outcome|use case` returns nothing. There is no path from "I
want to classify support tickets" to a pipeline. The console assumes the user
already knows the answer and is looking for the button.

**Why the gap is small.** Three of the four pieces a guided path needs are
built:

1. ADR-0106 D4 already assigns intent to the pipeline (*flow = needs, pipeline =
   intent, profile = machinery*), so the metadata has a decided home.
2. The run page is already the workspace the design asks for — #2019 Wave C
   shipped per-step stage, percent, ETA, cost and log tail. What it lacks is
   human-readable stage names; it renders `run-prep-split`, not "Preparing data".
3. The recommender already explains itself (#2028), which is the hard half of
   guidance.

**Stage-by-stage coverage of the reviewed design**, against the tree:

| Stage | Today | Gap |
|---|---|---|
| 1 Goal | `POST /pipelines/suggest`, `core/recommender.py`, `training/recommender/{priors,rules}.yaml` — recommends preset, config, priced GPU plan | no *approach* recommendation (fine-tune vs RAG vs prompt) |
| 2 Data source | `ingest` (Postgres/MySQL/ClickHouse/Snowflake/BigQuery/httpx), `s3`, `dataset_synth`, **and file upload** — `routers/datasets.py:68` stages uploaded bytes at full fidelity for `dataset-readiness/import`, with UI at `DatasetsView.tsx:819` | the three entry points are not offered as one control |
| 3 Prepare | `profile`, `scan_all`, `dedup` (MinHash), `pii_scan` (Presidio+regex), `decontam` (n-gram), `anonymize`, `curate`, `format`, `split` (stratified **and** group-aware), `prep_gate`, `prep_review`, `provenance` — deeper than the design proposes | class balance, label validation against declared categories, missing-value handling |
| 4 Baseline | `eval/score` + `eval/delta` | delta fires on `condition: regression_baseline_provided` — the baseline is *supplied*, not *produced* |
| 5 Fine-tune | `training/finetune`, QLoRA/LoRA, hard cost cap | — |
| 6 Evaluate | `delta`, forgetting check, champion/challenger, `gate` | — |
| 7 Deploy | `promote` (human-gated), `serving/serve` | — |

## Decisions

### D1 — Outcome metadata lives on the pipeline, refining ADR-0100 D1

ADR-0100 D1 put `suite:<name>` on flows with a per-flow front-matter override.
Since ADR-0106 D1 (only a declared pipeline runs), grouping the catalog by a
flow tag groups things a user cannot start. The authoritative home for the
grouping metadata of the **runnable surface** moves to the pipeline:

```yaml
outcome: "Classify support tickets more accurately"
suite: model
```

ADR-0100's flow tag is not removed — it still describes the component, and the
resolved view may show it. But the catalog, the goal picker, and any "what can I
do here" surface read the **pipeline's** fields. Additive; a pipeline without
`outcome` renders ungrouped, exactly as today.

### D2 — A goal is declared, never inferred

`outcome` is a literal string in the pipeline YAML, authored by whoever ships
the pipeline. The console matches on it by search and by suite. No model is in
the routing path: an LLM may *help a user phrase* a search, but the mapping from
phrase to pipeline is data the pipeline author wrote. Consistent with cairn's
standing rule that the model drafts and the engine decides.

### D3 — One guided way in; no second run page (amended 2026-09-10)

*Original text: "No wizard. The pipeline's legs are the stages."* That was right
about half of the prototype and wrong about the other half, and it borrowed a
governance argument to reject a layout.

Split the prototype's five steps by what already exists. **Run** and **Result**
duplicate the run page — which, with D4's labels and D9's tab, is already richer
than a simulated progress bar. Those are not built. **Choose → Configure →
Review** are pre-launch: they are where a non-technical user is actually lost,
and nothing sequenced them. Those ship as one route (#2168) that composes what
the slices built — D5's grouping, D10's fold and controls, the launch dialog's
existing preflight — and hands off to `/pipelines/{id}` through `POST /start`.

Why that is not a "second start door": ADR-0106's one door is `POST /start`, a
governance chokepoint, and this route ends in it. It is a second *entrance* to
the same door — a UX fact, not a governance one. The original D3 conflated the two.

Why it earns a route at all: the three capabilities shipped scattered across
three screens, and the owner looked at them and did not recognise a guided path.
The same capabilities in sequence read as guidance; on separate pages they do
not. For the audience this ADR exists for, the sequencing *is* the feature.

Two hard rules follow. The route **composes, never reimplements** — the
prototype's own `TypedField` is a second copy of D10's controls and is deleted
when #2168 lands. And it depends on **annotation coverage (#2169)**: a guided
route over unannotated pipelines is a guided route to raw leg ids.

### D4 — Every leg declares a human-readable stage label

A leg gains an optional `label:` (e.g. `Preparing data`, `Establishing
baseline`, `Fine-tuning`). Absent a label the console falls back to the leg id,
as today. The run page, the graph and the launch preview all render the label.
This is the single change that turns the existing run page into the guided
surface without new UI.

### D5 — The catalog leads with outcome; KIND becomes secondary

The pipelines gallery (`PipelinesView` → `PipelineGallery`) gains an outcome/suite
grouping as a **view**, beside the pack grouping it already has. **Pack stays the
default** until `outcome` has real coverage — verified 2026-09-10: 1 of 33
offerable pipelines declares one, so an outcome-default catalog would be one real
group beside eight fallbacks. No pipeline is hidden and no route changes.

*Corrected 2026-09-10.* This decision originally named `CatalogTab`, which renders
the pack **registry** (`/registry/catalog` — operator packs, flow packs, bundles)
and has no access to a pipeline's `outcome`. It also said "KIND becomes
secondary"; the KIND chips live on `CatalogTab`, so on the surface this decision
actually governs there is no KIND filter to demote. Both were errors in the
original text, found when #2147 was built.

### D6 — A pipeline that claims a delta measures both sides

Not a training rule. Any pipeline whose output is a *change* — `compress/distill`
(teacher vs student), `redteam/campaign` (pre- vs post-hardening),
`eval/score-a-model`, as well as the training pipelines — declares the baseline
evaluation as a leg that runs **before** the change, against a split the prepare
stage produced. `eval/delta` stops being conditional on a caller supplying a baseline.
Without this the user can train, score 77% against a 78% base they never
measured, and not know they went backwards. This is a pipeline-authoring rule,
not an engine change.

### D7 — Variants are separate declared pipelines

The general rule is ADR-0106 D1, restated for this context: a variant gets its
own declared pipeline rather than one pipeline branching at run time. Training's
classification / generation / extraction split is the **first instance**, not the
rule. Each declares its own prepare legs and its
own metrics (F1/accuracy · LLM judge + human review · field-level accuracy).
This follows ADR-0106 D1 rather than fighting it, and keeps every runnable
surface readable in git.

### D8 — Approach recommendation extends the existing recommender, as data

"You may not need fine-tuning — your goal is retrieval; use RAG" is a new rule
family in `training/recommender/rules.yaml`, not new code and not a new service.
It is **advisory and never routes automatically**: it renders as a card the user
can dismiss, in the same surface that already explains why a config was
suggested (#2028). Honours the packs-are-data rule.

### D9 — Before/after comparison is the one genuinely new surface

One input through **the two sides the pipeline already measured**, on the run's
results view — teacher vs student for distil, pre- vs post-hardening for redteam,
dirty vs clean row for dataset prep, base vs tuned for training. Which two legs
are the two sides is declared by the pipeline (`compare: {before, after}`), so the
view stays domain-blind. **Five** pipelines across four packs run the same body
twice, and three are genuinely before/after shaped — `compress/distill-compare`,
`training/closedbook-compare`, `training/verifiable-finetune` (corrected
2026-09-10; the original "17 across 6 packs" came from a grep that matched
comments). Enough to make this a cross-pack surface rather than a training
feature, which is the point. Nothing today does this, and for a non-technical user it is worth more than a metrics
table. This is the only item in this ADR that is new UI rather than labelling
and routing.

### D10 — Two experiences on one surface: `essential` and `kind`

The console must serve a non-technical user and an engineer from the same screen,
without becoming two products. Two additive input declarations do it:

- **`essential: true`** — this input is part of the few-clicks path. Everything
  else folds behind *Advanced options*. **Required-vs-optional cannot be the
  split** — but not for the reason first written here. `training/finetune`
  declares 56 inputs, of which **one** is required and 55 carry an explicit
  `required: false` (measured 2026-09-10; the original text claimed all 56 were
  required, an error from counting "no `default` key" as required). So the form
  asks for one field and then dumps 56 flat — and `base_model`, the second thing
  any person chooses, is `required: false`, so it appears in **neither** place a
  beginner looks. Only an author-declared `essential` puts it where it belongs.
- **`kind:`** — the semantic type (`dataset` · `model` · `connector` · `secret` ·
  `choice` · text), which selects a purpose-built control instead of a text box.

Both are **additive**: an input declaring neither renders exactly as it does
today, so no existing pipeline changes appearance. Every typed control keeps a
free-text escape, and must emit the identical string today's text box emits —
a picker that normalises differently would change what runs while every test
still passes.

"Some pipelines need a dataset, some don't" is then handled by **absence**: a
pipeline with no `kind: dataset` input never renders a dataset control. No
branch in the console asks what kind of pipeline it is.

## Rejected

- **A stepped wizard engine.** Duplicates the run page; creates a second start
  door; contradicts ADR-0106.
- **Free-text goal → LLM picks the pipeline.** Puts a model in the routing path
  and makes the catalog unpredictable. D2 keeps the mapping declared.
- **Hiding KIND entirely.** Operators and flow authors navigate by it daily.
- **One branching "customize a model" pipeline.** Becomes the 80-line wrapper
  ADR-0106 D2 was written to kill.
- **Auto-routing away from fine-tuning.** D8 stays advisory: telling a user they
  do not need the thing they asked for is a suggestion, not a redirect.

## Consequences

- The console gains a front door without gaining a second start door.
- Pipeline authoring gets one new obligation (`outcome`, leg `label:`) and one
  new rule (D6 baseline ordering). Both are additive; unlabelled pipelines
  render exactly as today.
- ADR-0100's suite tag stays valid but stops being the grouping key for the
  runnable surface. Anything that grouped by the flow tag must be re-pointed.
- D6 makes improvement pipelines longer and more expensive by one eval pass.
  That is the cost of an experiment rather than a hope, and the cap still binds.

## Sequencing

**Corrected 2026-09-09.** The earlier text said ADR-0106 S1 was in flight and
that D1/D4 should ride the S2 packs wave. Both S1 (#2138) and S2 (#2142) have
since merged to `integration/adr-0105`, so that window is closed: D1/D4/D10 now
need their own pass over the pipeline files.

Order:

1. **Slice 0 — the model fields** (`outcome`, `suite`, `label`, `kind`,
   `essential`, `compare`) plus serving them on `/pipelines/installed`. Python
   only, no console change, so nothing can regress. A golden baseline of all 40
   installed pipelines is captured and **vendored** *before* this lands — a
   baseline read through `git show <branch>:<path>` compares a file with itself
   the moment it merges and proves nothing (this defect was hit three times
   during ADR-0106).
2. **Readable leg labels** — the cheapest visible change, deliberately first: it
   proves the whole chain (YAML → model → payload → render) at the lowest risk.
3. **The disclosure fold**, then **the typed controls** (D10).
4. **The outcome catalog** (D5), keeping pack grouping and leaving the default
   view alone until `outcome` has coverage.
5. **Before/after** (D9).

D8 is independent and can run at any point. **D6 changes how runs execute** —
it makes a pipeline longer and more expensive by one eval pass — so it is not
part of the UI work and needs its own issue and its own go-ahead.

**Every new field is display metadata, never execution input.** None of
`outcome`, `suite`, `label`, `kind`, `essential` or `compare` is read by the
dispatcher, enforced by a test asserting the dispatch input dict is identical
with and without them present. A bug in any of them cannot change what runs.

**Prototype.** `proto/2144-guided-run` implements the whole path against a mock
generated from a live payload, at `/_guided` behind `import.meta.env.DEV`
(verified absent from the production bundle). The owner green-lit the design
from it on 2026-09-09. Its mock module is the contract slice 0 must satisfy.
