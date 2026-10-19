# ADR-0087: cairn SDK — a compiler frontend over records, never a runtime

- **Status:** Accepted (built; amended 2026-08-07 — see Amendment 1)
- **Date:** 2026-08-06
- **Brief:** docs/ideas/cairn-sdk-vocabulary-sample.md (the 15-word vocabulary,
  reviewed 2026-08-06 against the shipped code)
- **Related:** ADR-0084 (flows/pipelines as records), ADR-0068/0070 (config
  scoping), ADR-0086 (run-lifecycle hardening — `retries`/`timeout` compile
  onto H2), #1107 (call-site grammar), the standing "SDK needs its own ADR +
  prototype; cairn stays record-first" ruling (2026-07-29) — which this ADR
  discharges rather than overturns.

## Context

The prior SDK question was rejected because a Prefect-style SDK — flows *are*
code — destroys the record-first properties the control plane depends on:
content-hashing (`cairn lock`), admission over a diffable artifact, and
structural gate enforcement (the compiler reads a flow def to require an
approval gate on `external_write` stages; it cannot read arbitrary Python).

The vocabulary sample resolves that tension with one rule: **every word maps
1:1 to a record.** The Python surface is authoring sugar; the compiled YAML is
the artifact of truth. Review against the code found the vocabulary ~90%
veneer over shipped machinery — `step`→leg kinds (incl. `task`), `loop` with
`max_iterations`/`on_exhausted="gate"` (shipped verbatim), `Governed`/
`Operational`→config_schema split, `on=[cron, watch]`→triggers, `role:` models
(#352), preflight/events/gate-queue/pins/audit already served by the API — and
six genuine design holes, decided below. Two of them are runtime-semantics
gaps no compiler can paper over.

## Decision

An official `cairn_sdk` authoring package, built as a **compiler frontend**:

```
author classes ──cairn pack build──▶ pipelines/*.yaml + flows/*.yaml + lifted op wheels
                                     └── hashed by cairn lock; installed; RUN AS RECORDS
```

The runtime never imports author classes. The console, `cairn run`, admission,
and the audit chain see only records — an SDK-authored pack and a hand-written
pack are indistinguishable after build. This is the same projection tier as
`cairn flow export --to prefect|metaflow` (records project OUT to native
orchestrator artifacts): one IR, multiple frontends and backends.

### D1 — The record is the truth; the class is a view

`cairn pack build` compiles; the compiled YAML is committed/installed and
hashed. Regenerating from an unchanged class is byte-stable (deterministic
compiler). A pack may mix authored YAML and compiled YAML freely.

### D2 — `when` routes, `gate` stops — and the runtime learns to route

Today the YAML `when:` compiles to the plan's `gate` predicate, whose runtime
semantic is **fail-the-pipeline** (`gate_blocked` → `_fail_pipeline`, pinned by
`test_gate_fail_terminates_pipeline_e2e`). The SDK's `when` requires
**skip-and-continue**: a false `when` marks the leg `skipped` (a first-class,
non-failure verdict), and execution proceeds. Both semantics stay available:

- `gate:` (named, or predicate) — STOP: fail closed, a human/policy/data
  verdict is required to pass. Unchanged.
- `when:` — ROUTE: skip the leg, continue the pipeline.

A downstream input that references a skipped leg's output must be declared
optional in the wire (`consumes: {x: Report?}`) or the plan fails **at compile
time** — never a null surprise at runtime. This is new plan IR + bridge
behavior and ships before or with SDK P1 (the optional-leg pattern is
unusable without it).

**Shipped in #1251** (P0), with two notes from contact with the code:

- `?` is a YAML indicator, so the optional marker is written plainly in BLOCK
  style (`consumes:` / `  x: Report?`) and needs quoting in flow style
  (`consumes: {x: "Report?"}`) — the flow spelling above does not parse. The
  parser now says so in the error.
- The rule is stricter than "declared optional or fail": an *undeclared*
  `consumes` entry onto a `when:` leg also fails. `consumes:` is otherwise
  incremental (#1137), but silence about a wire that may produce nothing is the
  null surprise this rule exists to prevent.

### D3 — `until` binds to the LATEST iteration

`loop(..., until=<expr over step refs>)`: refs to steps that also occur inside
the loop body resolve to the **most recent iteration's** instance, never the
pre-loop snapshot (which is immutable history and would never satisfy the
predicate — the non-termination bug found in the sample). The compiler rewrites
the predicate against the loop-tail ids the bridge already maintains.

### D4 — One ordering truth: data edges; `graph` may only add

Dependencies implied by data refs (`redact = step(..., dataset=ingest.out.x)`)
are authoritative. `graph = (a >> b >> …)` may add control edges (sequencing
without data flow) but a `graph` that contradicts or omits a data edge is a
**compile error**, not a warning. No plan whose picture lies about its data.

### D5 — Lifted `def` steps declare capabilities; no inference

`@step def` bodies are lifted into private ops at build. They MUST carry
`requires=[...]` like any `@op`; touching `ctx.<capability>` not declared fails
the run (fail-closed, ADR-0053 discipline). The build lints the obvious case
(AST finds `ctx.X` with no `requires`) but linting assists declaration — it
never replaces it. Lifted ops get a synthesized ref
(`<pack>/_steps/<pipeline>.<step>`), risk-classed like any op (omitted risk =
`external_write`).

### D6 — `run()` submits by ref + hash; drift is rejected

`SupportFinetune.run()` does NOT execute the local class. It submits
`pipeline:<pack>/<name>` plus the class's compiled content hash; the server
rejects on mismatch with "local class differs from installed pack — rebuild/
reinstall". The local class is a *pointer with a checksum*, so CI and the
console can never silently run different pipelines.

### D7 — Every gate has a stable audited name

Anonymous data gates (`gate(prepare.out.stats.rows >= 1000)`) get
compiler-synthesized deterministic names derived from the predicate
(`gate-prepare-rows-ge-1000`), stable across rebuilds of an unchanged
predicate. The audit trail never shows `gate-3`.

### D8 — Scope of the vocabulary is closed

The 15 words are the whole grammar. No escape hatches into arbitrary code at
the pipeline tier (that is what ops/kernels are for). A construct the
vocabulary cannot express is a grammar proposal (new ADR), not a `**kwargs`.

## Consequences

- Record-first survives intact — strengthened, since the SDK makes records
  cheaper to produce than to hand-write.
- Two runtime features fall out for everyone, including YAML authors: `when`
  skip-routing (D2) and policy-injected structural gates (the sample's
  "cannot be removed here" cost gate — `Policy.require_gate(before=...)`
  inserting a gate leg at plan time).
- `retries`/`timeout` on `step` compile onto ADR-0086 H2 (#1244) — the SDK
  ships no retry machinery of its own.
- The client tier (`preflight/run/events/pending_gate/wait/lineage/audit`) is
  a thin typed wrapper over existing endpoints; no new server surface.

## Alternatives rejected

- **Code-as-flows (Prefect-shape SDK).** Rejected again, same grounds:
  unhashable, unadmittable, structurally ungateable.
- **AST capability inference for lifted defs.** Convenient, fail-open;
  contradicts the capability model's fail-closed core.
- **`when` as sugar over today's gate predicate.** Today's semantic fails the
  pipeline; optional legs (the sample's `benchmark`) would be unusable.

## Execution

| Phase | Slice | Issue |
|---|---|---|
| P0 | `when` skip-and-continue runtime semantics + optional wires (D2) | #1251 |
| P0b | Policy-injected structural gates | #1252 |
| P1 | Compiler: declarations → YAML (pipeline/step/gate/loop/flow builders, expression projection, D3/D4/D7), golden tests vs the multi-engine fixtures | #1253 |
| P2 | `@step def` lifting → private ops (D5) | #1254 |
| P3 | Typed client + drift guard (D6) | #1255 |
| P4 | `cairn flow export --to prefect\|metaflow` (same projection tier) | #1256 |

P0 is runtime work and benefits YAML authors independently; P1 is the
prototype the standing ruling asked for and touches zero runtime code.

**All six phases merged 2026-08-06/07** (PRs #1260 P0, #1270 P0b, #1265 P1,
#1274 P2, #1272 P3, #1273 P4).

## Amendment 1 (2026-08-07) — build record + three grammar gaps

Written after the full slate merged. The build kept every decision above; P2
additionally delivered the pieces the vocabulary needed that P1 had skipped:
`parallel(a, b)` (chain groups; a data edge between siblings is a compile
error under D4), the site vocabulary (`Environment`/`Connection`/`Policy`/
`Budget` in `cairn_sdk.envs` — `Policy.require_gate` rides the P0b
`pipeline_legs` injection; `Environment` compiles to a `load_profile`-valid
profile), the `cairn pack build` CLI, and the D5 runtime half
(`cairn.core.operator.guard` — `guard_context` enforces the declared
`requires` on every ctx access; `CapabilityNotDeclared` on dynamic access the
build lint cannot see). Two deliberate compile fences hold the D8 closed-scope
line: `async` lifted defs and lifted defs inside `loop(...)` are
`CompileScopeError`s, not half-features.

Contact with the record grammar surfaced three constructs the vocabulary can
express but the RECORDS cannot hold. Per D8, each is a grammar proposal, not a
compiler workaround — the compiler fails loud (or emits an honest build note)
rather than fudging semantics:

1. **No def-level data-STOP predicate (G1, #1275).** D7 names anonymous data
   gates, but post-#1251 the pipeline grammar has exactly two verdict forms:
   `when:` (ROUTE — skip and continue) and `gate:` (a named HITL leg). There
   is no per-step predicate that STOPS the pipeline on data
   (`gate(prepare.out.stats.rows >= 1000)`). Compiling it into `when` would
   invert fail-closed into fail-open — refused. Today the compiler raises
   `CompileScopeError` with the alternatives; the proposed grammar extension
   is a `predicate:` field on the gate leg (bridge-evaluated, fail-closed on
   false, no human queue), at which point D7's synthesized names attach to a
   real record.
2. **Pipeline-level `on = [cron(...), watch(...)]` has no record home (G2,
   #1276).** Schedule records live in their own store (#354 console crons;
   #992 watch triggers) keyed to deployments, not packs — the pipeline record
   has step-level `trigger:` but no entry-trigger block. The SDK captures
   `on=` and compiles it to a build NOTE naming the real wiring. Candidate
   homes, deliberately not chosen here: an `on:` block in the pipeline record
   that install materializes into the schedule store, vs. keeping triggers
   deployment-side with a `cairn pack build --wire-triggers` client step.
3. **Environment-level `Budget`/`models=` have no env-record home (G3, same
   class as G2 — recorded, not filed).** Budgets are runtime records (pipeline
   `governance.budget`, the #993/#999 series ledger); model roles live in the
   #352 registry, not the profile. The `Environment` builder compiles both to
   build notes naming the real wiring. This is likely correct permanently —
   budgets and role maps are live, mutable state, not installable records —
   so no grammar change is proposed; the note IS the design.

The captured-never-dropped rule is the load-bearing contract across all
three: an authoring construct without a record home becomes a loud note (or a
compile error), never a silent drop and never an invented semantic.
