# ADR-0084: cairn is a control plane — one flow concept (3 kinds), mandatory pack pipelines, runtimes as adapters

- **Status:** Accepted (execution epic #1122, phases 0–4)
- **Date:** 2026-07-28
- **Supersedes/amends:** ADR-0083 D5 (authoring surface), D6 (definition modes), D7
  (execution model) · **largely answers** #1120 (R6d: three composition models)
- **Source:** `architecture.md` (repo root, discussion draft distilled 2026-07-27/28)

## Context

ADR-0083's audit fixed connective tissue but left the top of the system ambiguous:
templates (compiled to LangGraph) composed everything, flows sat under a door node
inside them, bundles chained runs from outside packs, and the SDK's shape was
undecided. Three questions surfaced repeatedly and were finally settled by checking
the code rather than reasoning from the design:

1. **What orchestrates?** If a data→ml pipeline with zero LLM steps executes inside
   a LangGraph state machine, LangGraph is a mandatory dependency of everything and
   the park/poll machinery exists to work around a graph runtime holding a
   long-running leg. That inverts the platform-neutrality goal.
2. **How many kinds?** Proposals ranged from six (data/agent/ml/knowledge/event/
   process) to a universal flat node/edge IR. The packs contradicted both.
3. **What is the pack's runnable surface?** Templates, flows, and bundles each
   claimed it, with three grammars and three registries.

Three facts from the repo drove the answers: **no shipped template uses
`agent_loop`** (even `security_triage/triage-alert` is `type: prompt`); **one
Metaflow adapter serves both `kind: data` and `kind: ml`** (kinds do not imply
runtimes); and the **workflow registry has zero pipeline/bundle awareness** (any
entry path bypassing install is console-invisible — P8).

## Decision

### D0 — Flow-leg run identity (the open question from #1122 Phase 0)

A `flow:` leg dispatched directly to an engine is its own governed run and records
`template_ref = "flow:<pack>/<name>"` at admission. R2 (identity recorded at
admission, never re-derived) holds unchanged, console/insights grouping works with
no special case, and the `flow:` prefix makes the leg kind self-describing in the
audit trail. Rejected: reusing the flow ref bare (collides with template refs),
and a nullable `template_ref` (re-introduces the P7 attribution fallbacks R2 deleted).

### D1 — cairn is a control plane; runtimes are adapters

cairn owns identity, policy, gates/HITL, cost, lineage, artifacts, audit — what is
true *across* executions. Runtimes (builtin, Metaflow, Prefect, LangGraph, future
Ray/Spark) execute legs behind ONE waist: `validate / submit / poll / cancel /
result`, with the conformance suite as the protocol (ADR-0081 D3, unchanged).

**Kind boundaries are control-plane boundaries: LangGraph never holds a non-agentic
leg.** A pure data/ml pipeline loads neither LangGraph nor Temporal. LangGraph is
demoted from "the brain" to a peer adapter scoped to agentic legs, where it runs the
compiled BT.

### D2 — ONE concept: FLOW, three kinds

| Kind | File | Body | Runs on |
|---|---|---|---|
| `data` | `flows/*.yaml` | `stages:` (kernel list) | engine adapter |
| `ml` | `flows/*.yaml` | `stages:` + resource hints | engine adapter |
| `agentic` | `flows/*.md` | BT frontmatter + prose body | LangGraph adapter |

The word **"template" is retired** at the authoring surface; `templates/` remains a
loader alias during transition and internal identifiers (`template_ref`) keep their
names — the concept unifies now, string churn is out of scope. A pack's `flows/`
holds **any number of files in any mix of kinds**.

`data` and `ml` share a body family (one adapter serves both); they stay separate
kinds because their governance defaults and engine routing differ. The agentic body
differs because **who decides execution order** differs: stage lists are
author-declared; a BT is author-declared; only *content* is model-decided.

**Rejected kinds:** knowledge (attached state — RAG backends + pack corpora, via
`knowledge:`), event (an entry point — `on:`/watch), process/durability (a derived
property per segment).

### D3 — No ReAct/tool-loop in the workflow model

An LLM step is a single-shot `prompt` node: one call, prose from the flow's markdown
body, structured output validated against the declared schema. The author declares
control flow; the model decides content only. `agent_loop` leaves the workflow
grammar; the `cairn ask` copilot keeps its loop as product code
(`cairn/ask/agent.py` calls `create_react_agent` directly), outside the pack grammar.

> ⚠️ **Correction (2026-07-28, during #1132).** This decision originally claimed
> "zero pack usage — migration cost is zero". That grep covered `cairn/packs/`
> only. Repo-wide, **`examples/templates/kitchensink-investigation.md` uses
> `type: agent_loop` and is exercised by `tests/core/test_examples_bundle.py`**.
> The removal is therefore staged: the builder is DEPRECATED (warns, naming this
> decision) and removed once that example migrates to `state_machine` + a
> `prompt` node. The decision stands; the cost estimate was wrong.

### D4 — `pipelines/*.yaml` is the top orchestrator, mandatory per domain pack

Every `kind = "domain"` pack ships ≥1 pipeline (knowledge and operator packs are
exempt — they are resources, not runnable domains). The pipeline is the pack's
composed, runnable surface and **the registry's installed unit**; flows remain
independently runnable (`cairn run <pack>/<flow>`).

Steps are legs: `flow:` | `template:` | `task:`, plus `gate:`, `when:`,
`depends_on:`, `parallel:`, `loop:`, per-leg `environment:`. **Each leg is its own
governed run** — its own admission, budget reservation, policy evaluation,
approvers. Wires are typed artifact references (`<step>.<produced>`), checked at
compile time.

### D5 — Packs are self-sufficient; bundles are removed

A domain pack never references another pack's executables (flows/templates/
pipelines). Exactly two shared layers remain: **operator packs/kernels** (code,
versioned wheels) and **knowledge packs** (data, attached as environment resources).
The sharing model is *share code and knowledge, copy the YAML* — a 6-line stage list
duplicated over the same shared kernels is cheaper than a dependency graph.

Consequences: the **bundle concept is removed** (format, `examples/bundles/`, and
`cairn bundle` CLI deleted — hard cut; the PipelineBridge machinery survives as the
pipeline executor) · **`[uses]` pack-dep pinning is retired** (#1104, merged
2026-07-27 — no purpose without cross-pack refs) · `subgraph_node` is restricted to
**same-pack** composition (the gated-subgraph splice machinery stays; incident uses
it 3×) · the exactly-one cross-pack reference in the tree (`training/finetune` →
`dataset-readiness/check`) is internalized.

### D6 — Durability is derived per tier; no Temporal for data/ml

| Tier | Durable substrate |
|---|---|
| **Pipeline** | The **run store IS the checkpoint** — completed legs are terminal runs with persisted outputs; resume = plan re-evaluation; zombie legs caught by the reconciler (#976); loop position persisted. **No Temporal at this tier.** |
| **Agentic leg** | LangGraph checkpointer + `interrupt()`/resume; Temporal per-node activities **optional**, an environment choice |
| **data/ml leg** | Engine-owned: builtin `.done` markers, Metaflow step checkpoints + `resume` (both live-proven). Control-plane side holds only the run record + engine handle; restart re-attaches by handle |
| **Gates** | `awaiting_approval` status + stores — durable at every tier by construction |

This generalizes ADR-0083 D7 ("durability derived per segment") one tier up, and
upholds ADR-0082 D2 (`retries=0` cairn-side; `@retry` owns step retries). A pure
data/ml deployment ships without Temporal *and* without LangGraph.

### D7 — Closed loops are bounded and escalate to a human

`loop:` steps carry `until:`, a **mandatory** `max_iterations` (unbounded fails
lint), and `on_exhausted: gate` by default — non-convergence lands in the approval
queue with iteration history rather than failing silently or looping on spend. Each
iteration's steps are fresh governed runs (per-iteration budget reservation);
implementation is **runtime unrolling** over the DAG planner, so the plan stays
acyclic and the audit trail stays linear.

### D8 — The compiler is amended, never replaced

The deterministic BT compiler is load-bearing infrastructure (content-addressed
compile, structural gate enforcement, Temporal splice #569/#581, checkpointer
attach, preflight/UIContract). Any proposal requiring gate enforcement, durability,
or replay to be re-proven on a new surface is rejected on that ground alone.

Net effect of this ADR on `compiler.py`: it **shrinks** — loses the `agent_loop`
builder and the cross-pack visibility branch, gains nothing. The pipeline compiler
is a separate small unit beside the evolved `engine/bundle.py`.

### D9 — Console noun is Pipelines

The "Workflows" surface becomes **Pipelines** — the runnable business unit with the
Run form, triggers, and budget. Flows demote to components: visible in the pack
Library, and expandable inside the pipeline detail (agentic leg → its graph, flow
leg → its stages — the #1077 combined-graph pattern one tier up). Runs view is
unchanged; legs group by pipeline id.

## Consequences

**Gained:** platform neutrality is structural, not aspirational (a deployment can
carry neither LangGraph nor Temporal) · one authored concept instead of three
grammars · governance applies uniformly because every leg is a run · the pack is a
self-contained, shippable domain module.

**Lost / paid:** cross-pack reuse of executables (deliberate; costs some YAML
duplication over shared kernels) · `[uses]` work retired two days after merging
(sunk cost does not get a vote) · one more tier for authors to understand
(mitigated: single-flow packs ship a one-leg pipeline).

**Risk:** the pipeline tier is new surface area on proven machinery — mitigated by
evolving `engine/bundle.py` rather than writing a parallel executor, and by the
three restart tests in #1130.

## Alternatives rejected

1. **Template as the universal composer** (held for much of the discussion,
   retracted) — makes LangGraph a mandatory dependency of pure data/ml pipelines and
   uses a graph runtime to hold long-running legs.
2. **Six flow kinds** — conflates domain (knowledge/ML), entry point (event), and
   durability (process) with execution semantics; only the last is a kind axis.
3. **`kind: agent` as a standalone leaf format** — templates already carry prose
   natively; a second prose container is redundant.
4. **Universal flat node/edge IR** — a plain `edges:` list cannot carry gate-splice
   semantics (composition changes shape when a child contains a gate), and control
   flow genuinely differs by kind.
5. **Runtime hints (`preferred: [spark, ray]`)** — puts engine names in author
   hands, violating ADR-0081 D4; and "the compiler may ignore it" makes portability
   fail quietly. Capability requirements + environment selection replace it.
6. **Event-bus chaining between flows** — dissolves the governance chokepoint and
   fragments run identity; composition stays a declared plan.
7. **Objectives/planner as a runtime layer** — a synthesized graph is unauditable;
   objectives PROPOSE, pinned content-hashed plans EXECUTE.
8. **Converting flows to markdown frontmatter (one file format)** — a stage list has
   no prose body; the `.md` wrapper would be ceremony. Format follows content.

## Execution

Epic **#1122**, five phases: P0 this ADR (#1123) · P1 format + registry install +
CLI (#1124–#1126) · P2 leg executor + flow legs + gates + loops + durability + lint
(#1127–#1131) · P3 compiler cleanup + unified `flows/` + 6 pack pipelines + bundle
removal (#1132–#1135) · P4 console + `on:` + typed artifacts (#1136–#1137).

Track 2 (template internals) is independent and already filed: #1117 (state
contract, keystone) → #1107 (inline bindings) → #1109 (pack migration); #1118 (one
expression language) feeds both tracks.
