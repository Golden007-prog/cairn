# ADR-0109 — Governed evaluation, candidate optimization and typed decisions

- **Status:** PROPOSED — maintainer decision pending. Creating this record does not approve implementation, adoption, paid experiments or production deployment.
- **Date:** 2026-09-22
- **Scope:** DSPy, Jev (TypeSafe AI), Ragas and standalone GEPA across agentic, data and ML workflows.
- **Evidence:** local HEAD `3a69f32fa7fee82cc75662776bbe6627b16d90a8` plus the inspected working tree; official upstream documentation accessed 2026-09-22. No remote-main parity or live benchmark claim.
- **Authority:** preserves ADR-0012, ADR-0020, ADR-0026, ADR-0028, ADR-0031, ADR-0034, ADR-0084, ADR-0103 and ADR-0106, and the frozen training baseline. Supersedes no accepted ADR. ADR-0108 remains a separate proposal, not a prerequisite assumed to be implemented.

## Context

Cairn needs evidence about whether AI workflows work, a controlled way to improve them,
and economical decisions inside bounded workflows. These are three distinct responsibilities:

1. **Evaluation:** produce measurements and diagnostic evidence.
2. **Optimization:** search for a better candidate using a bounded experiment.
3. **Runtime decisions:** classify, score or select among permitted alternatives.

None transfers authorization, workflow execution or production promotion to a model.
This record evaluates possible uses; scenario tables below describe proposals, not shipped features.

## Current architecture and verified evidence

Paths and lines are observations of this checkout, not permanent anchors.

| Evidence | Finding | Consequence |
|---|---|---|
| DOCUMENTED: [ADR-0012](ADR-0012.md), Decision C, lines 148–168 | Prompt optimization belongs in a pack producing versioned candidates for review, not autonomous engine mutation. | DSPy and GEPA must preserve that separation. |
| OBSERVED: `cairn/core/dispatch/start.py:179`, `cairn/core/dispatch/pipeline_launch.py:163` | Declared pipelines use the existing resolution and launch boundary. | No optimizer-specific launch door. |
| OBSERVED: `cairn/core/operator/runtime.py:948`, `cairn/core/dispatch/pipeline_bridge.py:1342` | Template/operator and task paths invoke the approval gate. | Provider selection cannot waive authorization. |
| OBSERVED: `cairn/core/dispatch/leg_executor.py:114` | Data/ml legs execute through the existing engine boundary. | A data/ml job must not acquire an agentic runtime just to call an optimizer. |
| OBSERVED: `cairn/services/llm/factory.py:97` | The LangChain model factory applies gateway routing, usage callbacks and the redacting model wrapper. | Third-party SDK calls do not automatically inherit these controls. |
| OBSERVED: `cairn/operators/official/eval/src/operator_eval/operators/rag_eval.py:88`, `:135` | `rag/eval` implements four Ragas metrics, with lexical fallback when Ragas is absent or raises. Output identifies `backend_used`; nonempty proxy scores can set `checked=True`. | Check backend and metric provenance, not just `checked` or metric name. |
| OBSERVED: `cairn/operators/official/eval/pyproject.toml:27` | Ragas is an optional dependency with a broad `>=0.2` constraint. | Current upstream API compatibility requires qualification. |
| OBSERVED: `cairn/core/eval/scorecard.py:53`, `:143`, `:174` | Scorecard rows and evaluation-budget helpers exist. | Reuse these carriers/helpers where applicable rather than introduce another evaluation store. |
| OBSERVED: source/dependency search and environment inspection | No DSPy/Jev/GEPA integration found in searched implementation. DSPy, Ragas and Inspect were absent from the inspected virtual environment; LangGraph, Temporal and Optuna were present. | Source support and installed/runtime support are different claims. No optional integration was executed. |

**DOCUMENTATION / IMPLEMENTATION CONFLICT — Ragas governance claim:** the module header
at `rag_eval.py:5` describes the Ragas backend as using the configured gateway, while
`_ragas_scores` calls `evaluate(ds, metrics=chosen)` at line 111 without explicitly
providing a Cairn-governed model or embedding adapter. This call site does not establish
the claimed redaction, budget or gateway coverage. Environment/default behavior might
supply routing, but that is not proof. Require a traced adapter path and rejecting tests
before using this backend for governed optimization or promotion. This ADR records the
gap; it does not silently change the implementation.

ADR-0012's prohibition on silent mutation remains authoritative. Its rationale must not
be interpreted as saying all DSPy use necessarily destroys replay: upstream documents
compilation as returning a new program, with save/reload support [S1]. Immutable inputs,
recorded execution and explicit promotion are compatible with offline optimization.

## Technology assessment

| Technology | Verified upstream role | Proposed Cairn role | Limits |
|---|---|---|---|
| DSPy | LM programs with signatures/modules and optimizers for instructions, demonstrations or supported weight tuning [S1]. | Optional bounded program-optimization implementation. | Not Cairn's compiler, durable runtime or public contract language. Metrics and holdouts remain essential. |
| Standalone GEPA | Reflective search over textual candidates using evaluation feedback; usable without DSPy [S2]. | Candidate prompt/configuration optimization around an existing evaluator. | Mutation scope must be constrained; general ability to optimize code does not authorize changing Cairn code or policies. |
| Ragas | Application evaluation, including retrieval/answer and agent/tool-use metrics, plus test-data generation [S3–S5]. | Evidence producer through the existing evaluation boundary. | Upstream capabilities exceed Cairn's current four-metric wrapper; scores are not truth or promotion authority. |
| Jev | Hosted typed questions over state: Choice, Score and Noul; Choice/Score expose distributions and confidence [S6–S7]. | Optional semantic classification/scoring provider in approved workflows. | Typed values can be semantically wrong. Hosted access, versioning, privacy and workload performance require qualification. |

DSPy, GEPA and Ragas have public source repositories [S9–S11]. Their exact releases,
transitive dependencies and licenses must be recorded before packaging. No public
open-weight/self-hosted Jev distribution was verified in this review. Do not label Jev
an open-source model or assume its hosted API can replace a chat-completion interface.

### Choosing DSPy versus standalone GEPA

Use DSPy when the experiment benefits from explicit, composable LM modules and tuning
instructions/demonstrations across them. Consider its GEPA optimizer when diagnostic
feedback is available, or another optimizer when the objective is primarily scalar [S1].

Prefer testing standalone GEPA first when Cairn already has a useful prompt artifact and
evaluator: a constrained candidate transformation can avoid translating the workflow into
a second program representation [S2]. This is an architectural inference, not a measured
maintenance or performance advantage. Compare both on the same objective and budget.
Do not adopt two optimization stacks without a demonstrated need.

Jev is not a substitute for either optimizer, nor a drop-in DSPy generative/reflection
model. Ragas may supply an objective to an optimizer, but the final evaluation must remain
independent. The four tools do not need to be installed together.

## Proposed decision

**D1 — Keep three responsibilities separate.** External libraries produce measurements,
candidate artifacts or semantic predictions. Cairn owns admission, authorized execution,
recorded identity, lineage, deterministic threshold policy and reviewed promotion.

**D2 — Optional implementations, existing boundaries.** Put Python adapters and optional
dependencies in operator implementations. Domain packs compose pipelines and supply domain
rubrics/defaults; profiles bind implementations; environment wiring supplies credentials.
Reuse installed-pack discovery. No new core registry, store, runtime, entry-point group,
workflow grammar or mandatory base dependency is proposed. Concrete package placement
requires a scoped implementation plan before approval.

**D3 — Optimize artifacts, never the live engine.** Input is a pinned candidate, reviewed
dataset, evaluator specification and resource budget. Output is an immutable candidate
plus experiment evidence. Allow mutation only of declared fields, such as task prompt
text or examples. Governance policy, permissions, executable graph structure, tool allowlists
and deployment bindings are outside the search space. Training and promotion stay separate.

**D4 — Evaluation identity is part of the evidence.** Record implementation, version,
metric definition, judge/model revision, rubric, dataset/split identity, sample coverage,
failures, cost and aggregation. Required Ragas evidence cannot be satisfied by lexical
fallback or a skipped evaluator. Missing, partial, nonfinite or wrong-backend results
produce explicit unavailable/failed evidence under the reviewed acceptance policy.
Do not silently reinterpret existing legacy results or alter fallback behavior in this ADR.

**D5 — Typed model decisions remain advisory to authorization.** Jev may choose only from
an already-authorized candidate set and return recorded evidence. Cairn validates the
result and resolves it to existing bindings. Confidence does not grant permissions,
approve a model, certify data rights or authorize a destructive tool. Always define
abstention/unknown behavior and provider-failure handling. A peaked distribution does not
prove in-domain calibration [S7].

**D6 — Govern every nested call.** Cover student, teacher, reflection, judge and embedding
calls, retries and fallback through reviewed existing service seams. The LangChain factory
alone does not cover non-LangChain SDKs. Preserve redaction, credentials, accounting,
request/workspace attribution, cancellation and audit. Demonstrate refusal before a new
call exceeds its reservation; report in-flight usage honestly. No duplicated policy or
budget engine is authorized. A missing seam is an explicit architectural blocker.

**D7 — Evidence before adoption.** Initially run offline/shadow experiments with no action
or promotion side effects. Acceptance needs independent task-quality evidence, total cost,
latency distributions where applicable, failure/recovery tests and maintained dependency
compatibility. No universal confidence cutoff, throughput limit or production-readiness
claim is adopted here.

## Agentic workflow scenarios

All scenarios are PROPOSED. Run them through existing declared pipelines, bounded template
nodes or approved task operators; do not add a DSPy ReAct loop to the workflow grammar.

| Scenario / problem | Tool and approach | Output / success evidence | Failure boundary |
|---|---|---|---|
| Retrieval answers omit evidence or include unsupported claims | Ragas measures retrieval and answer quality; DSPy or GEPA proposes query/answer prompt candidates. | Held-out groundedness, retrieval relevance, correct citations, task success, cost. | Faithfulness to supplied context does not prove the context is true. |
| Tool selection is inconsistent | Jev ranks/selects from authorized tools; Ragas evaluates recorded tool calls against reviewed expectations. | Correct tool/argument selection and end-to-end outcome; unauthorized tools refused independently. | A valid tool name can still be inappropriate; no execution permission from a score. |
| Repeated bounded-agent failures | GEPA uses sanitized diagnostics to propose task-prompt changes; DSPy is an alternative for modular programs. | New candidate resolves held-out cases without regressions; unchanged graph/permissions. | Do not optimize against raw private traces or expand loop limits to improve a score. |
| Too many uncertain responses reach users | Jev supplies an uncertainty signal; deterministic routing requests more evidence or human review. | Risk-versus-coverage curve, false-accept rate and review burden. | Low confidence, timeout or unknown provider response follows an explicit safe branch. |
| Agent quality is assessed only by final text | Ragas adds tool/goal metrics to trajectory evaluation. | Correct outcome plus trace evidence, tool constraints and side-effect checks. | A high goal score cannot compensate for an unauthorized action. |

Ragas's upstream agent/tool metrics are candidates for integration; the existing `rag/eval`
operator does not expose that full upstream surface [S4].

## Data workflow scenarios

| Scenario / problem | Tool and approach | Output / success evidence | Failure boundary |
|---|---|---|---|
| Unstructured records need semantic categories | Jev produces bounded categories and review priorities. | Versioned label proposals with source IDs; per-class precision/recall and calibrated abstention. | Model labels are weak supervision until validated; no automatic deletion. |
| Extraction and normalization prompts vary by source | DSPy or GEPA optimizes the task prompt against reviewed examples. | Schema-valid records plus field-level semantic accuracy across held-out sources. | JSON validity does not prove units, dates or entity identity are correct. |
| Production failure logs are hard to curate | Jev triages; deterministic eligibility/redaction checks and review select examples. | Curated, versioned dataset with label provenance and exclusion reasons. | Logs/traces are evidence, not ground-truth labels or permission to train. |
| Retrieval dataset changes may reduce quality | Ragas compares fixed query sets across index/chunking candidates. | Per-slice retrieval and answer metrics, paired comparison, dataset/index identities. | Keep corpus-derived synthetic tests separate from independent holdouts. |
| Evaluation coverage lacks rare scenarios | Ragas generates candidate test cases; reviewers validate expected outcomes. | Reviewed additions tagged as synthetic, with source lineage and coverage rationale. | Generation and judging by the same model can reinforce the same error. |

These tools do not replace schema validation, exact deduplication, joins, storage engines,
PII controls or dataset-rights checks. Keep record processing in the existing dataflow
engine; exchange references/descriptors with the control plane rather than raw rows.

## ML workflow scenarios

| Scenario / problem | Tool and approach | Output / success evidence | Failure boundary |
|---|---|---|---|
| Fine-tuning is proposed before testing cheaper improvements | DSPy/GEPA compares reviewed prompt candidates against the frozen baseline. | Quality/cost comparison showing whether prompt-only improvement suffices. | A prompt gain is not weight training; do not relabel it as SFT or RL. |
| New model/adapter may regress a RAG or agent application | Ragas evaluates the same application/dataset with baseline and candidate. | Paired application metrics alongside task tests, training diagnostics and applicable model benchmarks. | Ragas is not a replacement for loss, perplexity, safety tests or deterministic task oracles. |
| Training labels need quality triage | Jev prioritizes ambiguous/inconsistent examples for review. | Reviewed label corrections and measured error rates by slice. | Do not turn confidence into a training reward or pseudo-label acceptance rule without separate validation. |
| Candidate judge rubrics disagree with experts | DSPy/GEPA proposes rubric prompt changes; compare against independently adjudicated labels. | Held-out agreement, bias/slice analysis and evaluator version history. | Judge tuning and final candidate evaluation must not share the same held-out answers. |
| A lower-cost model needs task adaptation | DSPy first optimizes instructions/examples; supported weight tuning is a separate optional experiment. | Explicit model/prompt identities and independently measured quality, latency and cost. | Weight updates must use Cairn's governed training boundary and immutable candidate contract. |
| Production drift suggests another learning round | Ragas supplies quality evidence; Jev may categorize failures; GEPA/DSPy may propose prompt candidates. | Reviewed request for curation/optimization/training, with lineage to evidence. | No direct trace → retrain → champion replacement chain. |

None of these integrations supplies a GPU scheduler, distributed trainer, inference engine,
checkpoint compatibility guarantee or RL experience system. Jev is not an RL trainer;
Ragas scores do not become a valid RL reward merely because they are numeric.

## Composition and ownership

```text
Frozen input/artifact + reviewed examples + explicit experiment budget
                 ↓ existing declared pipeline launch/admission
       baseline evaluation (existing eval boundary)
                 ↓
      DSPy OR standalone GEPA candidate job
                 ↓ immutable candidate + trial evidence
      independent evaluation (Ragas where applicable)
                 ↓ existing verdict and review boundaries
      explicit approved promotion / reversible binding update

Runtime alternative:
authorized state + candidate set → optional Jev decision
  → validate / abstain / existing authorization → existing operator invocation
```

The optimized artifact must include its executable semantics: model configuration,
adapter/signature behavior and demonstration selection when relevant. Do not claim a
DSPy program can always be exported as a plain prompt with identical behavior. Prove
translation parity or retain a bounded, version-pinned implementation behind the existing
operator contract. Do not wrap an entire Cairn workflow as one opaque durable step.

### Contracts and twins

Affected contract families from [the contract index](../contracts/README.md): Data
(dataset/split/label provenance), execution (`Operation`, `OperationContext`, `PipelineDef`,
`DataflowSpec`, `Run`), artifact identity, evaluation/scorecards, reporting (`ReportSpec`),
pack/profile configuration and governance. This proposal does not change their public shape.
An implementation must map required metadata onto existing carriers first; any missing
public field, new manifest grammar or ownership transfer requires its exact contract delta
and maintainer approval. DSPy signatures do not become Cairn public capability contracts.

Operator class and `pack.toml` declarations must match. If dispatch behavior changes,
prove PipelineBridge/Temporal parity; if persistence changes, preserve SQLite/Postgres
and old-row hydration. No such changes are presently proposed. HTTP/OpenAPI/console
regeneration is not applicable to this documentation-only change.

### Invariant coverage and proof obligations

| Invariant / authority | Owner and existing proof pointer | Integration-specific proof still required |
|---|---|---|
| One declared-pipeline door, ADR-0106 | Dispatch; `tests/core/test_one_start_call.py` | New experiment enters this door; no hidden launcher. |
| Operator and aggregate flow approval, ADR-0034 / root invariants | Approval/dispatch; `tests/core/test_leg_approval_gates.py`, `tests/core/test_flow_leg_gate_guard.py` | Rejection before calls or side effects for the chosen task/template/flow entry paths. |
| Redaction, cost and provider wiring, ADR-0028 / ADR-0031 | LLM service/gateway; `tests/core/test_gateway.py` | Every nested SDK call is covered; custom adapters refuse bypass and budget exhaustion. |
| Dependency direction and compiler preservation, ADR-0084 | Engine/operator owners; `tests/core/test_architecture_boundaries.py` | No core import of optional provider/optimizer packages; data/ml path does not load LangGraph. |
| Identity, lineage and immutable candidates, frozen training baseline | Storage/control-plane artifact registration | Retry uses pinned identities; trial records distinguish retries; no raw rows in control-plane records. |
| Review before persistent improvement, ADR-0012 | Pack composition + promotion/review owners | Candidate cannot modify its own production binding; rejected or unknown evidence cannot promote. |

These are proof pointers and acceptance obligations, not claims that tests were run or
that new integration coverage already exists. Existing invariants remain required;
coverage for all proposed integrations is **UNKNOWN** until implemented and verified.
The ADR-0106 local development exception is not production admission evidence.

## Alternatives and trade-offs

1. **Existing manual prompts and deterministic evaluation only:** lowest dependency and
   governance complexity. Keep as the baseline; prefer it when measured gains are absent.
2. **Standalone GEPA plus existing Cairn artifacts/evaluators:** potentially smallest
   optimization seam; requires a useful evaluator and careful mutation constraints.
3. **DSPy module-based optimization:** useful for multi-module LM programs; adds representation
   and runtime-adapter compatibility work. Adopt only when it beats the simpler path.
4. **Ragas as optional evidence producer:** reuse current ownership, qualify backend identity
   and governance first. Do not add a parallel experiment database as the source of truth.
5. **Jev as optional provider:** benchmark for narrow decisions; preserve substitution and
   explicit abstention. Reject making a hosted model mandatory for admission or authorization.
6. **All four as a new platform inside core:** reject. It duplicates ownership without
   evidence that Cairn's existing orchestration and artifact boundaries are insufficient.

High risks: governance bypass through nested calls, evaluator gaming/holdout leakage,
incorrect automated decisions, and retry-driven double spending. Medium risks: framework
version drift, serialized-program compatibility, hosted-provider availability and maintenance
of multiple representations. Their blast radius must initially be limited to offline
candidate artifacts and shadow predictions. No performance benefit is assumed.

## Evaluation and delivery gates

1. **Fix the task and evidence contract.** Choose one scenario, dataset owner, eligible
   inputs, expected workload and acceptance criteria. Record unresolved privacy/access
   conditions for hosted use. Define quality, cost, latency and recovery limits before tests;
   this ADR invents no numerical thresholds.
2. **Qualify the evaluator.** Establish human/deterministic references, test missing-backend
   and partial-result behavior, and verify nested-call governance. Fix any adapter gap under
   separate scoped approval before optimizing against its scores.
3. **Run fair offline comparisons.** Freeze baseline, train/validation/test splits by source
   or time as appropriate. Compare DSPy and standalone GEPA under equal declared budgets;
   compare Jev separately with the current decision mechanism. Do not charge optimization
   cost to neither side; include it explicitly in amortization.
4. **Inspect robustness.** Report per-slice results, uncertainty and sample counts, poisoning/
   prompt-injection tests, unknown categories, unavailable tools, provider timeout, budget
   exhaustion, cancellation and restart behavior. Benchmark representative concurrency and
   data volume; avoid whole-dataset materialization as an unexamined scale assumption.
5. **Check artifact portability.** Pin library, model, tokenizer where applicable, rubric,
   prompts/examples, dataset/split and candidate digests. Separate semantic replay from
   exact stochastic reproduction. Record cache identity and workspace isolation.
6. **Shadow before production.** Measure end-to-end errors, decision coverage and review
   burden without actions. Select thresholds using independent validation, not marketing
   confidence claims. Promotion remains an explicit reviewed change with rollback evidence.
7. **Adopt only the useful subset.** A negative result may justify keeping the current
   implementation. Publish supported versions and remaining limits only after qualification.

No engineering estimate is given before a scenario, governed adapter seam and compatibility
baseline are selected. Current source/doc inspection is not an end-to-end qualification.

## Upstream reuse check and recommendation

- **A — LangGraph:** already owns the agentic graph/control-state role; no replacement or
  new tool loop proposed. Optimizers operate on bounded artifacts/modules.
- **B — runtimes/engines:** existing runtime/dataflow/executor boundaries own lifecycle,
  retries and execution placement. External evaluation/optimization is work within them.
- **C — ecosystem:** reuse optional evaluation packages and existing budget/reporting
  primitives. No new mandatory core dependencies; API/version compatibility remains open.
- **D — Cairn:** reuse `rag/eval`, scorecards, the launch door, approval, artifact and run
  carriers. Preserve existing evaluation ownership and avoid another registry/store.
- **Build versus wire:** primarily adapters, candidate/evaluation metadata mapping and
  proof checks. Any missing governed non-chat call seam must be reviewed explicitly;
  it is not permission to create a parallel enforcement path.

**Recommendation: PROCEED WITH CONDITIONS for a scoped offline evaluation after maintainer
acceptance.** Prioritize evaluator integrity, then compare standalone GEPA with DSPy for
one task. Assess Jev independently as a replaceable typed-decision provider. This record
proposes direction only; it neither adopts all four tools nor changes the frozen architecture.

## Primary references

Accessed 2026-09-22. “Current”, “stable” and repository main branches can change; these
links document capabilities, not tested Cairn version compatibility or performance.

- **S1:** [DSPy optimizer selection and compilation behavior](https://dspy.ai/current/diving-deeper/choosing-an-optimizer/).
- **S2:** [Standalone GEPA: evaluator-driven reflective optimization](https://github.com/gepa-ai/gepa).
- **S3:** [Ragas overview](https://docs.ragas.io/en/stable/).
- **S4:** [Ragas metrics catalogue](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/).
- **S5:** [Ragas test-set generation](https://docs.ragas.io/en/stable/) — test-data generation navigation; API/version qualification is still required.
- **S6:** [TypeSafe primitives](https://docs.typesafe.ai/introduction).
- **S7:** [TypeSafe confidence semantics](https://docs.typesafe.ai/confidence).
- **S8:** [TypeSafe launch claims and benchmark caveats](https://typesafe.ai/blog/introducing-system-one-models-and-jev). Vendor claims are not Cairn guarantees.
- **S9:** [DSPy source](https://github.com/stanfordnlp/dspy).
- **S10:** [GEPA source](https://github.com/gepa-ai/gepa).
- **S11:** [Ragas source](https://github.com/vibrantlabsai/ragas).
