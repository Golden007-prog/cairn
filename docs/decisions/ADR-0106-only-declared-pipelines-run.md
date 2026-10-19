# ADR-0106 — Only declared pipelines run; a flow is a component

- **Status:** ACCEPTED (owner, 2026-09-09)
- **Date:** 2026-09-09
- **Epic:** #2134
- **Supersedes ADR-0105 D1** (a bare flow ref synthesized a one-leg pipeline at the door) and
  **ADR-0101 D1** (triggers declared on the flow). Keeps ADR-0105 D2/D3/D5 (a run is a leg; one
  console noun; the `pipelines` flag is a no-op) and ADR-0084 D2/D4 (ONE flow concept in three
  kinds; the pipeline is the top orchestrator).

## Context

ADR-0105 made every start produce a pipeline record, synthesizing a one-leg definition in memory
for a bare flow ref. That fixed the record but left the **runnable surface implicit**: nothing in
git says which flows are meant to be run on their own, so a reader cannot tell a product from a
component. Reviewing the console on 2026-09-09 the owner asked the question directly — *should a
flow be allowed to run without a pipeline file, and what does the file enforce anyway?*

Two facts decided it. First, the packs already answer it: **8 of 28 installed pipelines are
single-leg wrappers** (`price-a-run`, `import`, `triage`, `export-model`, `closedbook-measure`,
`dpo-pairs`, `dpo-train`, `train-only`) — the pattern exists, it is just not universal. Second,
those wrappers are expensive: `dpo-train.yaml` is **80 lines, of which ~74 restate the flow's
inputs and wire them back one-to-one**; only 6 lines (budget, series, approvers, outputs) carry
anything the flow does not already say. A rule that mandates 80-line wrappers would produce
input-drift bugs at every flow change. So the rule ships **with** the mechanism that makes a
wrapper cheap.

**On GitHub Actions.** Its separation (an action has no `on:` and cannot be triggered) is correct
*for GitHub*, because an action is context-free — no runner, no repo, no checkout, no secrets. A
cairn flow is not: it declares its own input contract and config schema, the profile supplies
backends, the workspace supplies defaults. We therefore adopt GitHub's **invariant** (one run
concept; triggers on the top-level unit; the runnable surface reviewable in git) and reject its
**mechanism** (a hand-written wrapper that restates the component's interface).

## Decision

**D1 — Only a declared pipeline runs.** A run is always a pipeline run. A bare flow ref is
**refused** at every door (`POST /start`, `cairn run`, triggers, the console) with a message
naming the remedy: add a one-leg pipeline, or use the dev-loop path (D6). The in-memory
synthesizer is not deleted — it now serves **declared** one-leg pipelines (D2) instead of bare
refs.

**D2 — `$inherit`: a one-leg wrapper does not restate its flow.** A pipeline's `inputs:` may
carry `$inherit: <leg id>`, adopting that leg's declared contract; inherited inputs wire into the
leg by name, and any key the pipeline declares alongside `$inherit` overrides the inherited one.
`$hide: [...]` drops inherited keys the wrapper does not expose. Explicit, not implicit: an
omitted `inputs:` block still means "no inputs", so silence never changes meaning and the lint has
a token to check. `dpo-train.yaml` becomes ~14 lines.

```yaml
name: dpo-train
kind: pipeline
inputs:
  $inherit: train                 # exactly what the leg asks
  gpu: {default: A10G}            # …with this one pinned
  $hide: [extra, target_modules]
governance: {budget: {usd: 20}, series: dpo-train, approvers: ml-leads}
steps:
  - id: train
    flow: dpo-align               # no `with:` — inherited inputs wire by name
outputs: {model_uri: train.adapter}
```

**D2a — Annotating an inherited input does not take ownership of it (amended 2026-09-10).**

D2's replace-not-merge rule is right and stays: *a half-overridden spec — this file's default,
the flow's type, nobody's description — is a value no one file is responsible for*, and
`inherited_inputs` provenance depends on the rule holding. But it makes ADR-0107 D10 unadoptable
where it matters most: a pack author cannot add `essential:` to an inherited input without
restating its type and description, taking ownership of it, and dropping it out of provenance.
**Every training pipeline is `$inherit`-based**, so the typed controls (#2155) work and the
pipelines that need them cannot declare them.

The resolution splits along D4's ownership line rather than fighting it:

* **`kind:` belongs to the FLOW.** "This input is a dataset" is a *fact about the need*, and D4
  gives needs to the flow. Declared on the flow's own contract, it rides `$inherit` for free and
  no wrapper changes at all. Agentic flows already have somewhere to put it — their UI contract is
  JSON Schema. Data/ml flows do not, which is exactly what **#2139** is for; that issue is the
  enabler, not a coincidence.
* **`essential:` belongs to the PIPELINE, because it is intent.** Which few things a *person*
  chooses is a property of this pipeline's purpose, not of the component: one pipeline may pin
  `base_model` and not want it offered at all. It is declared beside `$inherit` as a reserved key
  that annotates without owning:

```yaml
inputs:
  $inherit: train
  $essential: [dataset_path, base_model, method]   # annotate, do not restate
  $hide: [extra]
```

`$essential:` follows `$hide:` exactly — a reserved `$` key that acts *on* inherited inputs
without claiming them, so the spec keeps one owner and `inherited_inputs` still records the flow.
Naming a key the contract does not contain is refused at load, as `$hide:` already is.

**Rejected: merging when the declared spec carries only display keys.** It creates precisely the
half-owned spec D2's comment warns against, to save a line that `$essential:` already saves.

Both keys are display metadata and must be stripped from `content_hash` at every depth — the
input-level strip that #2145 originally missed (fixed in `28f32065`), for the same reason.

**D3 — Triggers and hooks belong to a pipeline, never to a flow and never to a run.** A flow that
declares `triggers:`/`on:` is **refused at pack install**. (No shipped flow declares one today, so
this costs nothing.) A run is an instance; nothing attaches to it.

**D4 — One owner per concept.** A **flow** states *needs* (its body, input contract, `$config`
schema). A **pipeline** states *intent* (legs, wiring, gates, `governance` = budget · series ·
approvers, triggers, outputs). A **profile** states *machinery* (capabilities, bindings, MCP
servers, backends, which policies apply). **`pack.toml`** states *facts about the artifact*
(identity, compat, asset dirs, the operator catalogue, `[requires]`, `[provides]`). Config values
are the one deliberate overlap: a flow declares a need, and run > pipeline > profile > default
resolves it (ADR-0068/0070). Inputs were an accidental overlap; D2 removes it.

**D5 — Demo tiers move next to the flow.** `[[demo.<flow>]]` in `pack.toml` is per-flow sample
data living in the artifact manifest — the wrong file by D4's test. It moves beside the flow, and
a wrapper inheriting that flow's contract inherits its sample tiers, which is how the launch
dialog offers "Try with sample data" without any flow surface growing a Run button.

**D6 — The dev loop is not a run.** `cairn run --interactive | --mock | --dry-run` executes a flow
in-process, writes **no** run record, and says so in `--help`. This is cairn's `act`: iterate
locally, produce nothing governed.

**D7 — Strict is the default for the no-orphan invariant.** With D1, the chokepoint in
`RunDispatcher.run_template` (#2133) **refuses** a pipeline-less call rather than wrapping it. The
one remaining hole — a dispatcher wired without a bridge — is closed before the flip.

**D-hide (amendment, 2026-09-10, #2173) — Hiding is curation, not governance.** An admin can hide
any installed pipeline or flow **per workspace**, and the hide is an override on the EXISTING
`visibility` axis, not a second field beside it (D4: one owner per concept). It is stored as a
workspace-scoped rule in the feature store (`hide:<kind>:<ref>`, so it inherits that store's
persistence, RBAC and who/when history), and `/pipelines/installed` and `/flows` serve
`effective_visibility` — `internal` when hidden, the declared tier otherwise — beside the
unchanged declared `visibility`. Because every human surface already curates on the served tier, a
hide lands on all of them at once, and the admin screen can still say "hidden by admin" versus
"internal by pack".

**The launch door does not consult it.** A hidden ref still launches by ref from the API, the CLI
and a trigger; its past runs still render; D1's chokepoint is unchanged. Hiding answers "what is on
this workspace's shelf", which is a *product* question with clutter as its failure mode — the same
question `visibility` has always answered. "This must not run here" is a different question with
money and blast radius as its failure mode, and it is answered by policy (ADR-0068/0070 and the
pipeline gates), where a refusal is loud and cites the rule that produced it. Wiring one control to
both would give an admin a curation UI whose real effect is governance, and — worse — a governance
control an admin could silently defeat by knowing the ref.

## Consequences

- Packs ship a thin wrapper for every flow meant to be runnable; a flow with no wrapper is a
  component and honestly cannot be started. That is a product decision made visible in git.
- The Playground and the Flows library launch **pipelines**; no flow surface has a Run button.
- The 8 existing single-leg wrappers shrink to their load-bearing lines under `$inherit`.
- ADR-0105's `synthesize_flow_pipeline` keeps its job, driven by a declared file.
- A declared pipeline and a flow at one ref must resolve to the **pipeline**, not to today's
  ambiguity refusal.

## Alternatives rejected

- **Keep bare-ref synthesis (ADR-0105 D1).** Governed, but the runnable surface stays implicit and
  a reader cannot separate product from component.
- **A pipeline file per flow, written out in full (GitHub's mechanism).** ~14 more 80-line files
  whose inputs rot at every flow change; rejected in favour of D2.
- **Sandbox as a documented exception.** Zero first-run friction, but the rule acquires an asterisk
  that has to be explained on every surface; the owner chose no exceptions.
