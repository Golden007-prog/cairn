# ADR-0113 — Bounded ReAct components and repeated reasoning workflows

- **Status:** PROPOSED — the maintainer selected the two-pattern scope and requested this ADR. Exact authoring grammar, compatibility and runtime qualification remain implementation decisions requiring scoped review.
- **Date:** 2026-09-25
- **Owner:** Cairn maintainer
- **Evidence baseline:** local working tree at `3f67d348`, including uncommitted documentation. No implementation or runtime qualification is claimed.
- **Proposed amendment:** ADR-0084 D3, narrowly permitting bounded adaptive components inside declared workflows. Do not edit the locked ADR or silently restore the retired `flow.type: agent_loop`.
- **Preserves:** ADR-0084 D8 existing compiler; ADR-0103 reporting; ADR-0106 declared-pipeline launch and run identity; existing approval, policy, budget, redaction, secrets, audit and artifact boundaries.
- **Related proposals:** ADR-0111 independent contracts and capability-only domains; ADR-0112 bounded deployment profiles. Neither is made accepted by this record. ADR-0110 native export remains separate.

## Context

Cairn needs two workflow behaviors:

1. An authored workflow containing a bounded adaptive ReAct component. The outer process
   defines its stages; the component chooses permitted actions during an investigation.
2. An authored reasoning workflow composed of operators and individual LLM calls, with
   optional bounded repetition using explicit feedback and updated inputs.

A separate pure ReAct workflow/product is excluded. `cairn ask` is a copilot, not evidence
that domain packs can author either new behavior. An adaptive component is invoked only
inside a declared Cairn workflow, including a small workflow with input preparation and
result validation. There is no standalone agent launch door or agent pack kind.

“Authored” describes control structure, not reproducibility of LLM answers. A model's
confidence is not evidence of correctness. Execution success and a business verdict
such as reject or inconclusive are different outcomes.

## Current support and evidence

| Classification | Current behavior | Source |
|---|---|---|
| OBSERVED | Agentic state machines have individual prompt calls and authored sequence, parallel, guard and subgraph composition. | `cairn/core/engine/schema.py:155`, `:433`; `cairn/core/engine/compiler.py:1150` |
| OBSERVED | Prompt nodes store raw response content and set action status to success; resolving an output-schema reference is not validation of generated content. | `cairn/core/engine/compiler.py:202`, `:912` |
| OBSERVED | Pipeline-level loops already implement bounded iteration, convergence and exhaustion. | `cairn/core/engine/pipeline_loop.py:1`, `:126`; `cairn/core/engine/pipeline.py:567` |
| INFERRED / UNQUALIFIED | Those pipeline loops are a reuse foundation; they do not establish a supported repeated reasoning-template interface with feedback, candidate revisions and result validation. | Same loop sources; qualification required below |
| OBSERVED | Compiler refuses the retired agent_loop grammar. | `cairn/core/engine/compiler.py:157` |
| OBSERVED | Operator invocation owns the approval gate before executor selection. | `cairn/core/operator/runtime.py:941` |
| OBSERVED | Runtime tagging distinguishes pure routing from LLM/tool/IO effects for durable execution. | `cairn/core/runtime/base.py:1` |
| OBSERVED | The separate ask copilot builds a ReAct read agent. It is not a pack workflow. | `ask/src/cairn_ask/agent.py:153` |

Upstream already supplies graph execution and agent patterns: [LangGraph workflows and
agents](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/workflows-agents.mdx).
Reuse compatible pinned-library behavior; current online examples do not prove the pinned
version or every Cairn runtime supports the required semantics. Core remains the governed
integration layer rather than a new agent runtime implementation.

## Decision (proposed)

### D1 — Two composable patterns

**Pattern A: adaptive component inside an authored workflow**

```text
prepare context → adaptive investigation → validate evidence → approval/action
                      │
                      └─ model decision → validated capability call
                               ↑                 │
                               └── observation ──┘
```

Use for incident RCA, focused error investigation, adaptive retrieval, difficult review
subtasks and data/ML failure diagnosis. Initial delivery supports read-only investigation
capabilities. Read-only does not mean free or nonsensitive: authorization, data access,
redaction and aggregate spend still apply. Write-capable actions require a later scope.

**Pattern B: authored reasoning workflow with optional repetition**

```text
candidate + evidence → prescribed checks/review → validated evaluation
                           ↑                         │
                           └─ updated input/feedback ┘
```

Use for PR review, document extraction/repair, answer revision, error triage and evaluation
cycles. Each next step comes from authored transitions. Repetition has a named feedback
mapping, candidate/evidence revision, termination condition and maximum attempts. A repeat
must not mean asking about unchanged evidence until the model approves. Existing one-pass
workflows remain supported. No new deterministic-agent runtime or Loop Operator is added.

### D2 — Ownership and package structure

| Owner | Responsibility |
|---|---|
| Domain pack | Declared pipelines/flows, objective/prompt content, required capabilities, feedback mappings, workflow-specific interfaces, proposed limits and escalation paths |
| Independent contract distribution (ADR-0111 proposal) | Reusable capability input/output/config/report definitions; evidence and domain-result schemas where shared |
| Operator distribution | Actual business actions, provider adapters and content validators; implementation-specific options |
| Existing Core compiler/runtime integration | Structural grammar, bounded control flow, request validation, governed dispatch integration, state and lifecycle wiring |
| Profile/environment | Approved model/operator/backend bindings, credentials through existing scope, authorization and deployment ceilings |

Keep `pipelines/`, `flows/`, `schemas/`, `policies/` and `presets/`. A new `agents/`
directory, agent pack kind or registry is unnecessary. Composition-only schemas stay local;
shared meaning is referenced. A domain asks for capabilities, never concrete provider
implementations under the proposed ADR-0111 direction. Do not create an agent-specific
capability resolver while that shared migration is being implemented.

A generic action envelope is an engine protocol; an RCA result is a domain contract.
Do not put all domain schemas in Core or require a package per tiny schema. An adaptive
component is composition; it is not a new operator that owns another orchestration system.
Existing prompt nodes need a reviewed capability-lowering mapping during ADR-0111 migration;
this ADR does not imply current prompt nodes are already Operation subclasses.

### D3 — Minimal declarations and typed decisions

An adaptive definition declares objective instructions, input/result schema references,
permitted capability identities/revisions, observation/context bounds, execution limits,
and completion/exhaustion handling. The exact YAML/markdown form is not fixed by this ADR.

The model may produce a discriminated request to:

- call one permitted capability with schema-valid arguments; or
- finish with a candidate result satisfying the declared result contract.

The model cannot supply an import path, operator override, arbitrary next graph node,
new permission, secret value, workflow definition or executable code. Declared capabilities
are resolved and pinned through the existing approved composition/binding mechanism.
Validate required support before effects. Invalid requests cannot become successful steps;
any repair attempt is bounded and charged to the same aggregate limits.

Every LLM result that controls a branch or tool call is parsed and validated before it
updates actionable state. Schema validity does not establish factual validity; explicit
validators and required evidence determine the business outcome. Uncalibrated confidence
or an LLM judge alone cannot authorize deployment or remediation. Jev and other evaluator
implementations remain optional integrations, not mandatory control-plane dependencies.

### D4 — Govern every action and bound the whole task

Adaptive calls use the same operator invocation, scoped credentials, redaction, approvals,
audit and configuration authority as authored calls. A library tool node must not directly
call an SDK or bypass `invoke_operator`. The component's allowed set is intersected with
deployment authorization, not treated as a grant.

Required bounds include model turns, tool calls, elapsed time, aggregate spend and retained
context/observation size. Count retries and nested calls against applicable aggregate
limits; do not reset limits on each turn or resume. Separate turn count from tool-call
count. First delivery may serialize tools; parallel tools are not required for acceptance.
Domain requests may be reduced by deployment ceilings. Preserve existing reservations and
settlement; identify any hard-cap limitations before claiming a strict spending guarantee.
No new policy evaluator, budget service or secret-injection mechanism.

### D5 — Feedback and outcome semantics

Repeated reasoning passes consume explicit prior results and feedback plus the candidate
revision/evidence snapshot. Define whether a pass reruns the whole body or an authored
subset; the model cannot invent the rerun scope. An unchanged-input/no-progress outcome
must stop or escalate rather than manufacture acceptance.

Keep lifecycle outcome separate from result verdict. The internal result envelope must
represent completion, inconclusive evidence, exhausted limits, failure and cancellation
without silently marking them all success. These do not automatically become new global
Run status values; map to existing lifecycle states through reviewed compatibility rules.
Human escalation belongs to the authored workflow's existing approval/gate mechanism.

### D6 — Persistence, trace and runtime qualification

An adaptive component may appear collapsed in the parent diagram, but its turns remain
inspectable. Reuse existing run identity, runtime checkpoints, events and artifact storage.
Record stable component/turn/call identities, capability/operator pins, validated requests,
observation references, result verdicts and cumulative usage. Redact data; do not require
private model reasoning or copy large datasets into control-plane state.

Checkpoint the decision and action completion boundaries. Resume must not blindly rerun
completed actions. Precisely document the crash window between an external effect and
recording its completion; do not promise exactly-once effects without backend support.
Cancellation and approval suspension propagate without starting new calls. The entire
adaptive loop must not become one opaque retried activity.

Qualify each supported runtime/backend combination separately. Extend existing adapters;
no universal Cairn runtime, new store or new pipeline owner. Unsupported combinations
must refuse admission. Data/ML engine boundaries remain unchanged. Bridge/Temporal owner
parity and SQLite/Postgres persistence twins apply wherever touched.

## Components to build or extend

Locations below are candidate ownership surfaces, not approval to edit every listed file.
Prefer existing helpers; introduce a focused module only where it reduces compiler growth.

| Component | Existing foundation | Required work | Proof of completion |
|---|---|---|---|
| C1: Generated-result validation | `engine/compiler.py`, `engine/schemas.py`, operator input/output validation | Parse/validate prompt results, typed decision errors and business verdict separation; cover single prompt, state-machine and map invocation paths | Malformed/unsupported output cannot drive routing or actions |
| C2: Reasoning repetition | `engine/pipeline.py`, `engine/pipeline_loop.py`, dispatch owners | Explicit feedback/candidate mapping, no-progress handling and documented use of existing bounded loops; introduce grammar only for demonstrated missing semantics | New evidence reaches the next pass; convergence/exhaustion/resume work |
| C3: Adaptive declaration | `engine/schema.py`, existing pack/schema discovery | Typed definition with permitted capabilities, result contract, limits and compatibility checks | Unknown refs/invalid declarations refuse before execution |
| C4: Adaptive graph assembly | Existing compiler and LangGraph integration | Small compiler helper assembling decision → validation → governed action → observation, plus finish/error edges | Visible turns with bounded termination; no compiler replacement |
| C5: Capability-call adapter | Shared composition/binding work, `operator/runtime.py` | Expose allowed contracts as model tools; validate request, resolve pinned implementation and call existing invocation with scoped context | Unknown/denied tools rejected; actual operator gate reached |
| C6: Aggregate limit integration | Existing budget, LLM factory and cancellation machinery | Carry counters/deadline/context bounds through retries and resume; settle usage consistently | Exhaustion/cancellation cannot start another call; no per-turn budget reset |
| C7: Checkpoint/recovery integration | `runtime/`, existing checkpointers and node tagging | Persist decision/action boundaries and stable call identity; qualify in-process/durable adapters individually | Crash, approval pause and restart cases pass on each advertised backend |
| C8: Trace/result projection | Existing runtime events, reporting and artifacts | Parent/component/turn/call correlation and typed outcomes using existing persistence | Trace reconstructs actions/usage; sensitive content is redacted |
| C9: Authoring and operator-facing inspection | Existing compiler explain/visualization and console projections | Display allowed capabilities, limits, nested trace and unresolved/exhausted outcomes; update API/types only if needed | User can distinguish success from incomplete investigation |
| C10: Conformance fixtures and docs | Existing test layers and architecture checks | One scripted PR-review iteration fixture and one scripted adaptive RCA fixture, with negative and recovery cases | Contract handoffs tested; mocks replace the model/backend, not the governed dispatch |

C5 depends on shared capability conformance decisions in ADR-0111/#2239; C1/C2 can be
scoped against existing authoring without prematurely adopting its proposed grammar.
Existing invocation gaps retain their issue owners (#2182/#1488). No support claim may
skip a failing required invocation path because a new adaptive fixture passes.

## Delivery sequence and acceptance

1. C1: establish generated-result validation with regression tests for the current raw
   prompt behavior; preserve valid existing content outputs through a reviewed migration.
2. C2: qualify authored review → feedback → repeat → completion/escalation using existing
   loop machinery. Repetition is not a generic agent autonomy feature.
3. C3–C6: implement the smallest read-only adaptive component using approved contract
   resolution and existing invocation; prove illegal action and limit refusals.
4. C7–C8: qualify recovery, cancellation, approval propagation where applicable and trace
   identity before advertising a durable runtime combination.
5. C9–C10: expose outcomes and examples, complete relevant runtime/owner conformance,
   architecture checks and documentation. Update the support matrix only with evidence.

Required rejecting scenarios: undeclared capability, bad arguments, malformed result,
unauthorized data access, expired deadline, exhausted aggregate limits, oversized
observations and unsupported runtime. Tool output is untrusted data and cannot expand
permissions. A scripted prompt-injection attempt must not authorize an extra capability.

Required lifecycle scenarios: final-turn convergence; inconclusive evidence; repeated
no-progress feedback; cancellation during a tool call; resume after model completion;
resume after recorded tool completion; failure during the external-call completion window.
Exact backend guarantees and unresolved crash windows must be reported, not hidden.

Compare the adaptive RCA fixture with an authored baseline for answer/evidence quality,
calls, cost and latency on representative cases before recommending adaptation broadly.
Agree workloads and acceptance thresholds before qualification; this ADR invents no
performance, accuracy or scale guarantees. Paid/live validation needs per-launch approval.

## Compatibility, alternatives and unresolved details

**Preserve:** existing one-pass flows and declared pipelines. Keep old `agent_loop` syntax
rejected until any explicit migration is agreed; its name does not silently acquire new
semantics. Version/pin graph definitions for durable histories and in-flight work.

**Simpler alternative:** authored branches and existing pipeline loops suffice when all
next actions are known. Prefer them for CI/CD, deployment and mandatory review checks.
Use adaptive execution where observations genuinely change the useful next investigation.

**Rejected:** pure ReAct as a separate runnable type; Loop Operator; agent registry/store;
new universal runtime; arbitrary pipeline/tool discovery during a run; recursive agents;
agent-generated workflow execution; mandatory Jev; opaque retry of an entire investigation.
Write-capable adaptive tools and agent-triggered child pipelines require a later proposal.

Before coding, resolve exact grammar, feedback binding semantics, decision/result schema
compatibility, where each aggregate bound is enforced, and the initial qualified runtime
set. State where the pinned graph libraries are sufficient and where adapter work remains.
These are bounded implementation decisions; they must not expand the two-pattern scope.
