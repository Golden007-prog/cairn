# ADR-0083: Re-architecture for the pipeline SDK — core simplification, pack redesign, three-decorator authoring

- **Status:** Proposed (no slice scheduled; sequencing-by-evidence in D8)
- **Date:** 2026-07-23
- **Supporting documents** (committed with this ADR, `docs/audits/`):
  - [rearchitecture-problem-inventory.md](../audits/rearchitecture-problem-inventory.md) — 27 code-verified problems (P1–P27), no solutions
  - [primitive-ownership-map.md](../audits/primitive-ownership-map.md) — who implements what vs who should; 11 mismatches
  - [sdk-target-design.md](../audits/sdk-target-design.md) — goals, philosophy, authoring surface, execution model, open questions
  - [pack-redesign.md](../audits/pack-redesign.md) — target pack format, inline bindings, rulings, migration
  - [pipeline-sdk-sample.py](../audits/pipeline-sdk-sample.py) — the worked one-file example

## Context

A full-day architecture audit (2026-07-23, main `835c25e`) established that cairn's
primitives are sound but their *connective tissue* is not: governance enforcement is
split-brain (the PolicyEngine is not wired into the per-operation gate chokepoint —
`approval/resolver.py:83` accepts it, both call sites pass `None`), domain logic is
trapped in core registries (778-line python-function registry), four expression
dialects exist for one concept, ~15 hand-rolled store triads ride ~13 DB env vars, and
the pack authoring surface (75 operation call-site files, 12 manifest section types)
makes both human onboarding and any future SDK impossible without freezing these
defects into a public contract. Separately, a design review of an external
"universal pipeline runtime" proposal (ml_pipeline.py) confirmed cairn's architecture
converges with independent designs on ~70% of decisions while exposing the same gaps.

## Decisions

### D1 — Goals & philosophy (binding for all subsequent slices)

Twelve goals with measurable targets and five principles (sdk-target-design §0). The
five principles: **(1)** governance is never traded but must cost zero author lines
(derive/default, never drop); **(2)** batteries included, sockets standard; **(3)**
optimize the paved road, allow the dirt road; **(4)** own the record, delegate the
work; **(5)** simplicity beats completeness. Conflict rules are pre-decided (§0).

### D2 — Ownership rule

Cairn owns what makes a run *governed, identified, composable* (contracts, specs,
registries, gates, lineage, policy, environment); delegates what makes it *execute*
(reasoning loops, durable replay, distributed compute, training math, vector search,
OAuth, gateways); where cairn writes code it orchestrates an OSS lib. The 11
today-vs-should mismatches (ownership map) are the re-architecture's exact scope;
everything else is settled and out of scope.

### D3 — Core re-architecture fixes (ordered by product risk)

- **R1 One enforcement chokepoint** *(pull forward regardless of the rest)*: wire the
  PolicyEngine into `resolve_runtime_gate`; single shared evaluator; first-class
  ApprovalStore replacing the `_approvals`-in-LangGraph-state record (P1, P2, P3, P4).
- **R2 One run identity**: `template_ref` written at admission; delete the console's
  3-way attribution fallback (P7).
- **R3 One config plane**: every read through `effective_backends`; `OBS_*` env vars
  become overrides consumed in exactly one resolver (P9, P10, P11).
- **R4 One storage substrate**: a workspace-scoped document-store API over
  SQLAlchemy/asyncpg with memory/sqlite/postgres drivers; the ~15 stores become
  namespaces (P12, P13, P14).
- **R5 One API composition root**: app factory mounting routers; console routes leave
  the triggers package (P15, P16).
- **R6 Connective tissue**: one expression language (P20) · typed state contract +
  declarative `output:` projections that empty the python-function registry (P17,
  P18) · inline operation bindings (P19) · one GraphIR walker (P21, P5) · one graph →
  three lowering targets (P26) · install-then-run shell (P8) · engine entry-point
  registry.

### D4 — Pack redesign

Per pack-redesign.md: minimum viable domain pack = 2 files; `kind` required
(`domain|operator|knowledge|provider`, replacing `type`+`kind`); assets by convention;
`[uses]` versioned pack deps (new); operator manifests generated from decorators;
`operations/` collapses to inline bindings (~75 → ~10 shared survivors);
**rulings:** `triggers/` removed (the pipeline's `on=` is the trigger definition) ·
`skills/` transitional until `on=` lands · pack `policies/` become
**shipped-as-proposals** (env must adopt; fixes the inverted always-inherited
precedence in `pack/profile.py:117-121`) · operators reach flows via the two-face
pattern (one core, `@operator` + generated data-plane face) with constrained
auto-lift (`requires ⊆ {artifacts, lineage}`). Data contracts (schemas, schema_hash
store, bundle wire validation) are unchanged in substance; only their production may
be generated.

### D5 — SDK authoring surface: three decorators

One decorator per kind of authorable content, everything existing is a value:

| Author | Decorator | Reference (value) |
|---|---|---|
| code | `@task` | `task.ref("op/name", ...)` |
| prose (docstring = prompt) | `@agent` | `use("pack/template", ...)` |
| wiring (inline stages or `source=` YAML) | `@flow` | `flow("pack/flow", ...)` |

Gates are declared, never authored: `gate("name", show=[...])`. `Pipeline` subclass
with class attrs replaces `@pipeline`. **No `@kernel` in the pipeline SDK** — a
`@task` referenced inside a `@flow` is auto-lifted to the data plane when
`requires ⊆ {artifacts, lineage}` (loud compile error otherwise); pack build emits
operators' data-plane faces automatically under the same rule. `Inputs`/`Outputs`
classes generate schemas (Run form + report projection). `cairn build` materializes
the file into standard pack records — the record runs, never the live object
(discovery-by-import at build time only).

### D6 — Definition-mode ladder + native escape hatch

For flows: (1) stage-list inline/YAML — portable, per-stage governance; (2) stage-list
+ namespaced `engine_overrides` — opaque decorations, adapter-validated; (3)
**`flow.native("metaflow", source=...)`** — hand-written engine-native flows are
ALLOWED: file is hashed/locked/shipped (identity & reproducibility hold), boundary
contract mandatory (schemas + risk, default `external_write`), engine-pinned
(compile-checked against the environment), one opaque node in the graph, optional
stage-event instrumentation. Same ladder for agents (`agent.native(graph)` — live
objects, additionally unhashable → author-bumped version). Environments may forbid
rung 3 (`Policy.reject_native()`; `Policy.reject_engine_pins()` implies it).

### D7 — Execution model: no new orchestrator

The pipeline `graph` never executes — the materializer (a compiler) lowers it:
task/agent/gate segments → template BT (LangGraph; Temporal wrap **only if** an
agent/gate is present); flow segments → door → engine via submit/poll/cancel/result
(pull-based, stateless, adopt-don't-resubmit); `detach()` → bundles. **One execution
owner per piece of work; durability derived per segment, never declared** — a
pipeline with no agent and no gate needs no Temporal at all. Engine artifacts are
derived by adapters in two phases (`prepare` once per flow-version×env — incl. the
worker probe that kills the #1075 bug class; `submit` per run). Engine-specific
config is three-tier: portable spec / `engine_overrides.<engine>` / environment
`*_engine_config`. Context crosses planes by value; artifacts are the only
inter-plane currency; lineage and cost are recorded control-side.

### D8 — Sequencing by evidence (given zero current users)

1. **Do regardless** (product hygiene, small): R1, R2, R3.
2. **Do for conversion** (thin slice): `cairn export` + `cairn run <file>` over the
   existing flagship — tests the SDK's time-to-first-value hypothesis without the
   full surface.
3. **Do on pull**: full authoring surface, engine adapters (Metaflow #1078 first),
   brain-runtime seam — each when a user, contributor, or prospect concretely needs it.

### D9 — Core-services contract: attachable to any workflow kind, adjustable only via the environment

The cross-cutting services (identity, lineage, audit, cost, policy/gates, storage,
artifacts, memory, RAG, eval/judge, triggers, MCP) are **core-owned services** with
exactly **three attachment mechanisms** — always-on (identity, lineage, audit, cost,
trace: author writes nothing) · declared capability handles (`requires=[...]` →
ctx handle, undeclared access raises: artifacts, storage, connections, secrets) ·
agent attachments (memory, knowledge, tools, model role — meaningful only where a
reasoning loop exists). **Adjustment belongs exclusively to the environment**
(backends swap implementations; policies adjust behavior; model roles bind models);
a service is never configured per-stage beyond its attachment. This is the contract
that keeps "batteries attached, not assembled" true across all four work kinds and
keeps integration cost at N services × 1 spine. Full matrix:
`docs/product-definition.md`. The R-fixes of D3 are what make this contract
enforceable (R1 policy chokepoint, R4 storage substrate, R6 state contract).

## Rejected alternatives

- A new **PipelineRuntime** walking the graph (ml_pipeline.py's shape) — a third
  orchestrator duplicating LangGraph + Temporal.
- **Decorators as the governed record** (runtime introspection by import) — cannot
  fail closed; build-time materialization instead.
- **Universal mega-IR** covering every engine's features — the lowest-common-
  denominator trap; three-tier config instead.
- **Placement engine / magic runtime selection** — explicit environment mapping is
  the auditable, governable form.
- **`wait(handle)`** in the engine contract — anti-durability; pull-based poll only.
- **Type hierarchy (Task/Agent/Flow/Job/Human/Service) in the runtime** — semantics
  live in protocols and doors, not a class taxonomy; 6 concepts → 3 decorators.
- **Wrapping N engines up front** — adapters on pull behind the proven seam.

## Consequences

- The SDK must not ship before its D3 prerequisites (R6 minimum, R1 strongly wanted)
  or today's defects freeze into a public contract.
- Pack migration is mechanical and incremental (`ref:` back-compat; codemod for
  ops→inline; no flag day) — see pack-redesign §6.
- 11 open questions are recorded (sdk-target-design §6) — each must be decided
  before its slice ships; none destabilize the design.

## References

Problems P1–P27 (inventory) · ownership mismatches Δ1–Δ11 (ownership map) ·
ADR-0034 (approval), ADR-0036 (runtime layering), ADR-0069 (bundles), ADR-0070/0071
(config/presets), ADR-0080/0081/0082 (data plane, engine protocol, ML flows).
