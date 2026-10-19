# ADR-0077 — Governed LLM red-teaming: adversarial safety as a governed workflow

Status: Proposed (2026-07-18)

Relates to / builds on: ADR-0026 (pack contract — a workflow = operator packs + a
DATA-only template/knowledge pack, the shape this follows), ADR-0075 (run outcome
channel — attack-success-rate rides `output.outcome`, `cairn gate` reads it),
ADR-0076 (loop-aware core — `series` budget caps the campaign, D4 watch re-runs the
suite, `cairn gate` blocks a promotion), ADR-0010/0012 (notification-only /
propose-not-apply — the two human gates obey this), ADR-0061/#509 (RAG + ambient
knowledge — the attack corpus), the eval pack (`eval/judge` via deepeval — reused by
the classifier), the LLM gateway (LiteLLM/OpenRouter — the target-model probe path),
and the `agent_loop` flow type (ReAct — the escalation node). Companion analysis:
the capability audit that produced this ADR (cairn is *packaged* data/train-centric,
but the engine + governance spine are domain-agnostic).

## Context — the gap this closes

cairn's shipped flagship templates cluster in **data-readiness, training, compression,
serving, and incident/ops**. That makes cairn *present* as a data/training platform,
even though the engine (three flow types: `prompt` / `agent_loop` / `state_machine`),
the operator/capability registry, and the governance spine (gate · approve · lineage ·
cost · audit · eval) are workflow-neutral. To prove — and extend — that breadth, we
want a workflow that is (a) high-value, (b) manifestly *not* data/training, and (c) the
archetype of "you would never let this run ungoverned."

**LLM red-teaming** is that workflow. An adversarial-safety campaign probes a target
model for jailbreaks, prompt injection, data exfiltration, harmful-content generation,
and bias, then produces a robustness verdict. Every property that makes it hard is a
property cairn already governs:

- The attacks are **dangerous and sensitive** → they must be *approved before firing*
  and the findings *approved before publishing* (ADR-0010/0012).
- Agentic escalation (mutate-and-retry) is **unbounded spend** → it must be *budget-
  capped* (ADR-0076 `series`).
- Compliance/audit requires **reproducibility + evidence** → every attack→response is
  *hash-chained and lineage-linked to the model version under test* (ADR-0047).
- A robustness bar must **block a bad model from shipping** → attack-success-rate
  becomes a domain verdict on the outcome channel, gating promotion via `cairn gate`
  (ADR-0075/0076).

The market gap is real: existing red-team tooling (garak, PyRIT, promptfoo) *generates
and runs* attacks but does not *govern* them — no approval gates, no enforced spend cap,
no hash-chained audit, no promotion gate. That governance layer is exactly cairn's moat.

## Decision

Add red-teaming as a **thin domain pack riding the reused governance spine** — the same
build-vs-wire ratio every existing cairn workflow follows. cairn does **not** become an
autonomous attacker: the human (or their CI) launches the campaign; cairn makes each
turn approved, budgeted, audited, and reproducible. Attack *generation* and *escalation*
run through the existing `agent_loop` + `series` budget so they can never spend or fire
unbounded.

### The governed campaign (one `state_machine` template, `redteam/campaign`)

```
Scope&seed → Generate attacks → ⏸ approve plan → Probe target (gateway)
           → Classify harm → Aggregate ASR verdict → ⏸ approve publish → Report + suite
                    ↑______________ escalate: mutate & retry (agent_loop · series budget)
```

- **Scope & seed** — `RedTeamRequest`: target endpoint (via gateway), attack categories,
  seed corpus, budget, ASR threshold.
- **Generate** — `redteam/generate` builds an `AttackPlan` (templated + mutation) from
  the attack corpus.
- **⏸ Approve plan** — HITL gate; nothing fires until a human approves (attacks are
  costly/dangerous). `requires_approval = true`.
- **Probe** — fire each attack at the target via the LLM gateway; capture `ProbeResult`;
  cost-metered per call.
- **Classify** — `redteam/classify` → `HarmVerdict` per attack (jailbreak-success /
  harmful / refused), scoring via the existing `eval/judge` + deterministic rules.
- **Escalate** — an `agent_loop` node mutates partially-successful attacks and retries,
  **bounded by the series budget** (fail-closed at the cap) and a max-iteration cap.
- **Aggregate** — `redteam/aggregate` → attack-success-rate + severity, emitted on the
  ADR-0075 outcome channel as the domain verdict.
- **⏸ Approve publish** — HITL gate before findings leave the run (sensitive content).
- **Report + suite** — `redteam/report` → cited findings; the plan is stored as a
  regression suite re-run on model change (D4 watch / schedule).

## What needs to be BUILT (net-new)

1. **`operator-redteam` pack** (official) — capabilities `redteam/generate`,
   `redteam/mutate`, `redteam/classify`, `redteam/aggregate`, `redteam/report`. Heavy
   attack logic reuses OSS behind an `[extra]` (lazy-imported, per the imports rule) with
   a deterministic fallback so the base install + tests stay hermetic — the exact pattern
   `operator-eval` uses with deepeval.
2. **`redteam_kb` knowledge pack** (DATA only: markdown + YAML) — a seed
   attack/jailbreak corpus, indexed via the existing RAG stack. Distinct from
   `security_kb` (which is MITRE ATT&CK for SOC alert triage — a different domain).
3. **`redteam` template pack** — `redteam/campaign` (full loop) + `redteam/quick`
   (single-pass, no escalation). Schemas: `RedTeamRequest`, `AttackPlan`, `ProbeResult`,
   `HarmVerdict`, `RedTeamReport`.

### Tool selection (build-vs-buy — licenses to verify at adoption)

Per the `/verify-upstream` gate, do NOT hand-roll attack generation. Candidate OSS,
Python-only (no Node runtime — rules out promptfoo's red-team plugins):

| Tool | Role | License (verify at adoption) | Infra |
|---|---|---|---|
| **garak** (NVIDIA) | probe/attack library + detectors | believed Apache-2.0 | pip `[extra]`, no server |
| **PyRIT** (Microsoft) | attack orchestration / mutation | believed MIT | pip `[extra]`, no server |
| deepeval (already pinned) | LLM-judge scoring in `classify` | Apache-2.0 | pip `[extra]` |

**Rejected:** promptfoo red-team (Node.js runtime — violates the no-Node constraint).
Verify garak/PyRIT licenses + no-mandatory-infra with a real check before adoption
(ADR-0076 struck Alibi Detect/Arize/promptfoo for exactly these reasons).

## What needs to be WIRED (reuse — no engine change)

- **`agent_loop`** — the escalation node (ReAct mutate-and-retry).
- **LLM gateway** (LiteLLM/OpenRouter) — target-model probe path + per-attack cost.
- **`eval/judge`** (deepeval) — scoring inside `redteam/classify`.
- **HITL approval gates** — approve-plan (before firing) + approve-publish (before
  findings leave the run).
- **`series` budget** — caps the campaign; without it, agentic escalation is unbounded
  spend. This is why ADR-0076 D5 exists.
- **Audit + lineage** — every attack→response hash-chained; campaign `parent_run`/linked
  to the model version under test.
- **Outcome channel → `cairn gate`** — ASR is the domain verdict; CI blocks a model
  promotion when `ASR > threshold`.
- **Watch / schedule** (ADR-0076 D4) — re-run the suite as a regression on model change.
- **Policy engine** — the redteam operators register `risk = high` / `external_write`
  so a governed environment forces approval + sandbox isolation.

Effort ratio: **~5 operators + 1 corpus pack + 1 template pack + schemas** are new;
everything governance/agent/gateway/eval/budget is wiring. A thin domain pack on a fat
reused spine — the same ratio as dataset-readiness, incident, and training.

## Non-goals

- **Not an autonomous attacker.** cairn never launches a campaign or escalates without a
  human-approved plan and a spend cap. No unattended continuous attacking.
- **Not a jailbreak cookbook.** The corpus is a governed test asset, gated behind
  approval + audit; it is not a distributable attack library.
- **Not a replacement for garak/PyRIT.** cairn *governs* them; the attack science stays
  in the specialist libraries (build-vs-buy).

## Consequences

**Positive**
- Concrete proof the platform is not train-locked: the moat (gate · approve · budget ·
  audit · reproduce) transfers to red-teaming unchanged.
- A compliance-grade robustness gate: `cairn gate redteam/campaign` blocks a model
  promotion on ASR — a first-class CI control no red-team tool offers today.
- Reusable substrate exercised in a new domain hardens it (gateway, agent_loop, series).

**Negative / risks**
- New OSS dependency surface (garak/PyRIT) — mitigated by `[extra]` isolation +
  deterministic fallback + license verification at adoption.
- The workflow *produces harmful content by design*; handling, storage, and the
  approve-publish gate must be treated as sensitive-data paths (redaction, access
  control, retention). This is the main safety-review item.
- Cost: agentic escalation is expensive; the series budget is a hard requirement, not
  optional.

## Open questions

1. Should ASR be a single scalar or per-category (jailbreak vs injection vs bias)? Likely
   per-category on the outcome channel, with an overall gate.
2. Corpus governance — who can add attacks, and does adding one require approval?
3. Target scope — only models reachable through the gateway, or also raw HTTP endpoints
   (a new probe transport)?
4. Whether `redteam/quick` should be `sandbox_safe` (mockable) for a Playground demo,
   like the dataset-readiness flagship.

## Status / next step

**Accepted, and partially shipped (2026-08-27).** See Amendment 2 for what
landed, what did not, and why the gap is tracked rather than closed.

---

## Amendment 1 (2026-08-26) — tool selection revisited; deepteam replaces PyRIT

This ADR's tool table was written 2026-07-18. Re-run through `/verify-upstream`
against the current ecosystem, one substitution and two confirmations.

### 1. Primary attack framework: **deepteam**, not PyRIT

`deepteam` (confident-ai) is Apache-2.0, Python 3.9–3.13, 40+ vulnerability
types and 10+ attack methods — and decisively, **it is built on deepeval,
which this repo already pins** (`operator-eval`'s `eval = ["deepeval>=3.0"]`,
driving `eval/judge`).

That matters beyond convenience. `redteam/classify` scores harm with an LLM
judge; `eval/judge` scores quality with an LLM judge. Adopting PyRIT would mean
**two independent judge stacks to calibrate and keep honest** — precisely the
work #1656 (judge scorecard) exists to do once. One judge family means one
calibration, one set of `untrusted_metric` semantics, one thing to explain to
an auditor.

PyRIT is not rejected on quality; it is rejected because it duplicates a
dependency family already in the tree.

### 2. **garak** stays — different job

Apache-2.0, NVIDIA-maintained, active (v0.15.1, June 2026). Its strength is
orthogonal to deepteam's: a probe/detector library reporting by OWASP category.
Adopt both behind the same `[redteam]` extra: garak for breadth-scan
(`redteam/scan`), deepteam for targeted attack + mutation
(`redteam/generate` / `redteam/mutate`).

### 3. **promptfoo** — rejection stands, with a second reason

Still requires a **Node ≥22.22 runtime**, which the original rejection named. A
Python control plane does not acquire a second language runtime for one
capability. Additionally it is now part of OpenAI — a consideration worth
recording for a governance product whose value proposition is neutrality about
which model you run.

### 4. Status

Moved from Proposed to **ACCEPTED** on this amendment. The build list in
"What needs to be BUILT" stands unchanged except for the substitution above.
⚠ This file was never committed (untracked since 2026-07-18); committing it is
part of landing the epic.

## Amendment 2 (2026-08-27) — the approval spine shipped; three operators did not

Epic #1722's first three slices merged: `operator-redteam` (#1729), `redteam_kb`
(#1737), and the `campaign`/`quick` pipelines (#1732).

**What is live is the part this ADR exists for — the approval spine:**

```
redteam/generate  →  ⏸ approve-plan  →  redteam/execute  →  redteam/aggregate  →  ⏸ accept-findings
(touches nothing)                        (fires + scores)     (ASR verdict)
```

The generate/execute split is the safety property, not a stylistic one. deepteam's
one-shot `red_team()` generates *and* fires in a single call and offers no seam to
gate on; `AttackSimulator.simulate()` is what makes approve-before-firing possible,
so that is what the operator wraps.

Rulings confirmed by building it:

1. **Gateway isolation is the integration.** Every deepteam *vulnerability* class
   carries its own `simulator_model`/`evaluation_model`, defaulting to hardcoded
   vendor strings — bare `cls()` calls OpenAI directly, outside redaction
   (ADR-0028), cost metering and audit. The `AttackSimulator`'s own model does not
   cover this. Four such escapes were found by installing the library, none by
   reading its docs; the bar for any future red-team dependency is **zero vendor
   calls with `OPENAI_API_KEY` unset**, proven by test.
2. **`risk` on the CLASS, always.** `redteam/execute` declares `external_write` on
   the class, not only in `pack.toml`. With the class defaulting to `read_only` the
   bridge's restart path would auto-refire an attack campaign at a system someone
   else operates — #1697 with a worse blast radius. Verified on the INSTALLED pack,
   because the sync/drift gates pass vacuously otherwise (CLAUDE.md rule 7).
3. **No evidence is not a pass.** `redteam/aggregate` treats errored attacks as
   *absent* from the ASR rather than as successes or failures, and returns
   `unknown` — never `pass` — when nothing scored. `quick`'s automatic gate demands
   `verdict == "pass"`, not `!= "fail"`, so `unknown` blocks. This is the #1720
   lesson (a judge outage read as catastrophic forgetting) applied one domain over,
   and it generalises: **an untrusted metric may block, it may never bless.**
4. **The corpus is a taxonomy, not a cookbook.** `redteam_kb` ships judgement
   criteria (what SUCCESS and REFUSAL look like per class), not working exploit
   prompts — deepteam generates the concrete strings at run time. Shipping payloads
   would age badly and help an attacker more than the grader.

**What did NOT ship.** RT-1 (#1723) specified six operators and delivered three.
The omissions are tracked individually so they are not lost in a closed issue:
`redteam/scan` (#1738, garak — a **buy**: ~100 maintained probes plus OWASP
categorisation, complementary to deepteam's targeted simulation),
`redteam/classify` (#1739 — a **thin build** on the already-installed deepeval and
the redteam_kb rubric; also a *fix*, since today's grade is a one-word
SUCCEEDED/REFUSED), and `redteam/report` (#1740 — a **build**, whose valuable half
is a re-runnable regression suite).

Consequently RT-3's **bounded escalation loop was not built and could not be**: it
needs `redteam/mutate`, which was one of the omissions. The merged campaign is
linear, accepted deliberately as v1, with the loop tracked at #1741. Open
question 3 above (raw HTTP targets) is now answered in code — `redteam/execute`
supports both the gateway and a direct `endpoint_url`.

## Amendment 3 (2026-08-27) — escalation is a bounded `loop:`, NOT an `agent_loop`

This ADR's Decision and its diagram specify escalation as *"the `agent_loop`
flow type (ReAct — the escalation node)"*. **That flow type no longer exists in
the pack grammar.** ADR-0084 D3 removed it:

> An LLM step is a single-shot `prompt` node… The author declares control flow;
> the model decides content only. `agent_loop` leaves the workflow grammar; the
> `cairn ask` copilot keeps its loop as product code, outside the pack grammar.

The builder is deprecated and pending removal. So this ADR was written against
a construct a later decision had already killed — an ADR conflict, recorded here
rather than silently worked around.

**Escalation is therefore the declarative `loop:` primitive** (ADR-0084 D7):
`max_iterations` mandatory and parse-enforced (`pipeline.py:290-294` raises
without it), `on_exhausted: gate` by default, already in service in
`readiness.yaml` and `retrain-cycle.yaml`.

The substitution is not a downgrade — it is the correct reading of this ADR's
own Context, which lists *"agentic escalation (mutate-and-retry) is unbounded
spend"* as the hazard and then proposed an agent loop to perform it. An LLM
deciding for itself how many times to re-attack a system someone else operates
is exactly that runaway. Under `loop:` the LLM writes the mutated attack TEXT
and nothing else; the bound, the exit condition and the approval are all
declared by the author.

Three rulings from building it (#1741):

1. **Every escalation round re-enters a human gate.** The pack's claim is that a
   person sees the attacks before they reach a system someone operates; a loop
   that re-fires derived-but-unread mutations would break precisely that claim.
   A campaign escalating three times queues three approvals — the intended cost,
   and the reason `redteam/quick` (single pass, no escalation) exists for
   unattended CI.
2. **Two independent bounds.** `max_iterations` caps the rounds; the `series`
   budget caps the spend. Either alone stops the loop, and `until:
   mutate.exhausted` exits early when a round finds nothing worth escalating —
   converging is the good outcome, not a case to spend the full bound on.
3. **Outcomes must accumulate explicitly.** A loop body step resolves to its
   LATEST completed instance (#1671), so the legs downstream of the loop would
   otherwise see only the final round and silently drop every earlier finding.
   `redteam/execute` gained `prior_outcomes`, wired self-referentially
   (`$default($steps.escalated.outcomes, $steps.execute.outcomes)`) — the same
   shape `continual-learning`'s train leg uses to continue a chain.

**One more gateway escape, found by installing deepteam rather than reading it**
— the fourth in this pack, and the same shape as the `simulate()` trap in
Amendment 2: `Roleplay.enhance()` opens with `self.simulator_model, _ =
initialize_model(simulator_model)`, whose parameter defaults to `None` and
resolves deepeval's DEFAULT VENDOR MODEL. `Roleplay.__init__` does not even
accept `simulator_model`, so binding the model at construction — sufficient for
the vulnerability classes — achieves nothing here. The model must reach
`enhance()`/`a_enhance()` itself, and only some classes take it: pure encodings
(`Base64`, `Leetspeak`) take none, LLM-driven ones (`Roleplay`,
`PromptInjection`, `GrayBox`) do. It is passed by signature inspection, so a
future rename degrades to "no mutation" rather than to "an ungoverned call".
