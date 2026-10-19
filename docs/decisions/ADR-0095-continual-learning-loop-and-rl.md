# ADR-0095 — Continual learning loop: GRPO/RL rung, replay, signal capture, champion/challenger serving

Status: **PROPOSED** (2026-08-25 — epic #1621, slices #1622–#1630).

**Renamed 2026-09-10 (#2175):** the flagship ships as `training/retrain-cycle` — named for
what it does, since "continual learning" names the EWC/GEM family §2.2 rejects.
`continual-learning-managed` (#1787 pattern) was retired to `tests/core/fixtures/pipelines/managed-loop.yaml`.

Relates to / builds on: **ADR-0082** (engine-neutral ml flows — the `flow_stages`
kernel surface the new trainer lands on), **ADR-0086** (checkpoint relay/resume —
inherited by every new trainer), **#1463** (DPO rung — the pattern `train.grpo`
copies), **#1456 / verifiable-finetune** (the proven measure→train→measure→gate
loop this ADR turns into a repeatable cycle), **#480** (continuous eval +
human-gated suggestions — the Understand/Steer half, turned ON not rebuilt),
**#1159** (pipeline `on:` arming — the loop-closing trigger dependency),
**#1459** (task-leg compute routing — blocks the in-pipeline serve leg),
**#893/#386** (the old "add DPO/GRPO via trl" asks — DPO half shipped in #1463;
the GRPO remainder is superseded by this epic). Datasets/judges/reviews surface:
#1465–#1468 (merged 2026-08-20).

Research basis (2026-08-25): production continual learning for LLMs is
**standard SFT/DPO/RL on PEFT adapters run repeatedly**, differentiated by data
engineering and serving infrastructure, not by novel learning algorithms.
Evidence: the CSUR 2025 LLM continual-learning survey (replay + PEFT + merging
dominate; classic CL algorithms like EWC/GEM impractical at LLM scale; Avalanche
absent from LLM practice); NVIDIA "Reuse, Don't Retrain" (continued training =
data mixing + LR schedule); Self-Synthesized Rehearsal (replay rows can be
synthesized when originals are gone); Trajectory's published C-LoRA stack
(standard sync RL/SFT on LoRA over vLLM multi-LoRA with in-place weight reload —
their edge is capture SDK + concurrency + approval-gated deploys, i.e.
infrastructure cairn's thesis already covers). The `verify-upstream` conclusion:
**wrap trl/unsloth/vLLM; build only capture, replay-as-data-op, the forgetting
gate, and the serving/canary tier.**

---

## 1. Context — what exists, what is missing

The inner loop is **proven, not hypothetical**: `verifiable-finetune.yaml`
(synth → prep → baseline → novelty gate → QLoRA train → serve-adapter HITL →
post eval → promote gate) was iterated to v8 on the 1M campaign, converging
~4 pts/cycle, every cycle through a human-owned gate. The mechanisms continual
learning needs are mostly present:

- **Sequential weight continuation**: `TrainSpec.init_adapter_uri`
  (`schemas/train.py`) continues an existing adapter with a fresh optimizer;
  the loaded `adapter_config` wins. DPO requires it.
- **Method rungs**: SFT (`train.qlora` → `train_entry`, unsloth+trl, GPU-proven)
  and DPO (`train.dpo` → `dpo_entry`, `DPOTrainer` with the adapter-disabled
  base as frozen reference). Both are `cairn.flow_stages` entry points.
- **Bounded iteration as grammar**: the `loop:` leg with mandatory
  `max_iterations` + `on_exhausted` (D7) — a continual loop cannot run away.
- **Forgetting is measured once, ad hoc**: the control split in
  verifiable-finetune (`baseline` vs `post` `control_acc` at the promote gate)
  is backward-transfer measurement hardcoded into one pipeline.
- **Governed data plane**: dataset import/versioning, judges, reviews
  (#1465–#1468) — the sink the loop feeds and reads.
- **Serving**: `serve/vllm` already launches `--enable-lora --lora-modules
  name=adapter` — one static adapter at launch; no multi-adapter, no reload,
  no canary.

Missing, in order of strategic weight:

1. **RL rung**: zero GRPO/PPO/reward code in the tree (grepped). Yet cairn's
   eval tier (`eval/judge`, `model/eval`, `eval/*`, the #1467 judge registry)
   is exactly a rack of reward functions — the expensive half of RL that other
   platforms have to build, already governed and versioned here.
2. **Production signal capture**: nothing turns real usage (accepts, edits,
   retries, scores, traces) into governed dataset rows. This is the ingress
   that feeds all three rungs: gold → SFT, pairs → DPO, scalar reward → GRPO.
3. **Replay/rehearsal**: forgetting is measured but not defended; no operator
   mixes prior-cycle data into cycle N's train slice.
4. **Champion/challenger serving**: promotion today ends at an MLflow registry
   row + a hand-run vLLM; no multi-adapter serve, no canary gate, no rollback.
5. **Cross-cycle lineage**: `producer_run_id` + `init_adapter_uri` record the
   chain; nothing renders adapter v1→vN as one series with per-cycle deltas.

## 2. Decision

**No new learning tech.** The engine stays unsloth+trl on PEFT adapters; the
continual-learning loop is composed from existing legs plus four bounded
builds. Specifically:

1. **The method ladder is signal-driven, one engine.** Gold outputs → SFT
   (`train.qlora`); preference pairs → DPO (`train.dpo`); scalar/verifiable
   reward → **GRPO (`train.grpo`, new)** via `trl.GRPOTrainer` on the same
   unsloth/QLoRA stack, same entry-point registration, same checkpoint
   relay/MLflow epilogue as the DPO rung. **Rewards come from cairn's own eval
   tier** through a reward-adapter shim (judges and verifiable graders exposed
   as `reward_funcs`). PPO/RLHF-with-reward-model is explicitly out of scope —
   GRPO is the industry default and needs no separate critic.
2. **Avalanche and classic CL algorithms are rejected.** Avalanche is a
   class-incremental research harness (vision benchmarks, plain `nn.Module`
   loops, no PEFT/chat-template/4-bit awareness); EWC/GEM do not scale to
   billions of parameters and the field does not deploy them for LLMs. What we
   adopt is the CL *evaluation vocabulary*: forgetting / backward transfer /
   replay ratio as first-class metrics.
3. **Anti-forgetting = replay as a data operation + a reusable forgetting
   gate.** `dataset.replay` samples a configured mix of prior-cycle rows from
   governed dataset versions into the new train slice (provenance recorded);
   `eval/forgetting` generalizes the verifiable-finetune control split into an
   operator + gate predicate (`control_acc(vN) ≥ control_acc(vN−1) − tol`) any
   pipeline can call.
4. **Adapter strategy: continue-the-chain is the default; merge is an explicit
   HITL event.** Cycle N trains from cycle N−1's adapter (`init_adapter_uri`).
   `model/merge` (fold adapter into base, start fresh) is a deliberate,
   gate-approved consolidation — because DPO/GRPO's KL anchor is the
   adapter-disabled base, merging silently rebases the reference; it must never
   happen implicitly.
5. **Serving tier: wrap vLLM multi-LoRA, don't build.** Extend `serve/vllm` to
   multi-adapter (`--enable-lora`, `max_loras`, dynamic adapter load/unload) so
   champion and challenger serve side-by-side behind one OpenAI endpoint;
   promotion flips an MLflow alias + endpoint default after a canary gate;
   rollback is the same flip reversed. Depends on #1459 for an in-pipeline
   serve leg; until then the serve step stays a HITL gate (as in
   verifiable-finetune today).
6. **The loop is closed with existing governance, not new autonomy.** One
   flagship `retrain-cycle.yaml`: `loop:` (bounded) over capture-window →
   replay-mix → readiness gate → train (SFT|DPO|GRPO by signal) → eval delta +
   forgetting gate → **HITL promote** → canary → feed back. Triggering uses
   pipeline `on:` once #1159 arms it; Understand/Steer uses #480 turned on.
   Judge-scored rewards and eval gains are `untrusted_metric` — a judge-only
   gain never auto-promotes; verifiable rewards (tests pass, closed-book
   grading against gold) are preferred for GRPO.
7. **Reproducibility prerequisite:** the GPU trainer deps (`_TRAINER_DEPS` in
   `images.py`, and the box/kaggle envs) get pinned versions — a training
   image that resolves different trl/unsloth on different days breaks both
   "reconstruct what executed" and GRPO importability guarantees.

## 3. Consequences

- Positive: cairn gets the full post-training ladder (SFT/DPO/GRPO) on one
  proven engine; the continual loop becomes a first-class, governed product
  story ("continual learning you're allowed to run in production") — the
  reference competitor's capture+approve+deploy loop, with governance native
  instead of bolted on.
- Cost: GRPO rollouts need generation on the training box — a materially
  heavier GPU profile than QLoRA SFT (vLLM-backed generation during training);
  budgets and `risk = paid_compute` framing must reflect it. The capture
  ingress and serving/canary tier are real projects, not switch-flips.
- Risk: reward hacking on judge-scored GRPO (mitigated: untrusted_metric rule,
  verifiable-reward preference, HITL promote); forgetting-gate tolerance
  tuning needs live cycles to calibrate; multi-LoRA serving concentrates
  champion+challenger on one GPU — a canary failure mode is now a serving
  failure mode too.

## 4. Rollout

Ordered by leverage: (1) pin trainer deps; (2) `train.grpo` kernel + flows +
pipeline (mirrors the DPO slice); (3) rewards-from-judges adapter; (4)
`dataset.replay`; (5) `eval/forgetting` + gate; (6) flagship
`retrain-cycle.yaml`; (7) multi-LoRA champion/challenger serving +
canary/rollback; (8) production signal capture ingress; (9) adapter-chain
lineage view. Dependencies consumed, not duplicated: #480 (turn on), #1159
(arming), #1459 (serve-leg routing).

---

## Amendment 1 (2026-08-25) — RAG-vs-train routing, replay v2, deferred distillation

Owner-reviewed additions after comparing this ADR against an external
continual-learning stack design. Three takes, one named deferral:

1. **RAG-vs-train triage is now an explicit principle.** New knowledge arrives
   in two kinds and they route differently: **factual updates default to
   retrieval** (the knowledge/RAG surfaces — cheap, instant, reversible);
   **behavioral/capability updates go to weights** (the SFT/DPO/GRPO ladder).
   Training facts *into* weights is a deliberate exception, taken only when
   closed-book capability is the point — and then it must prove itself through
   the novelty gate, which is precisely what `verifiable-finetune` exists to
   enforce. The flagship loop's readiness stage should classify incoming
   signal accordingly rather than assume everything trains.
2. **Replay grows a v2 strategy surface** (recorded on #1625): beyond
   `uniform | recency-weighted`, the spec reserves **hard-example replay**
   (rows the current model gets wrong) and **forgetting-aware replay** (rows
   whose score degraded between adapter versions — consuming exactly the
   per-row, per-version history `eval/forgetting` #1626 produces). `strategy`
   is an open enum from day one.
3. **The adaptive controller routes through #480, not new machinery**
   (recorded on #1627): a forgetting-gate breach emits a *human-gated
   suggestion* ("raise replay ratio next cycle"), never a silent knob
   mutation. The flagship pipeline keeps replay ratio / lr / method as plain
   inputs so suggestion→approve→relaunch can steer them. The lineage view
   (#1630) renders the **forgetting matrix** (versions × tasks, with a
   computed `best_previous − current` row) as the auditor's view.
4. **Deferred, by name: knowledge distillation** (teacher = previous model,
   `L = L_new + λ₁L_replay + λ₂L_distill`). Real technique, wrong first move
   here: with a frozen base + LoRA the base *is* the behavioral anchor, and
   DPO/GRPO already carry an explicit KL-to-reference term — while distill
   adds teacher inference to every training step (real GPU cost). Revisit
   only if replay + the forgetting gate measurably fail to hold the control
   split across cycles. This is a deferral, not a rejection — unlike
   EWC/Avalanche (§2.2), which stay rejected.

## Amendment 2 (2026-08-27) — first measured cycle: forgetting is real, and four rulings it forced

Cycle 1 (400-step QLoRA, no replay — nothing existed to replay) was measured
against its own base on the full 353-row harness (`training/closedbook-compare`,
one T4 session, judge deepseek-v4-pro, two independent runs with byte-identical
control accuracies). The numbers, from the governed record (#1695):

| split | base | cl-cycle1 | paired McNemar |
|---|---|---|---|
| eval (trained window) | 0.067 | 0.253 | p=0.012 |
| paraphrase_eval | 0.046 | 0.583 | p<0.0001 |
| control | 0.9515 | 0.8544 | p=0.013 (12 forgot / 2 gained) |

**The `no-forgetting` gate blocked, correctly.** The important part is the
*mode*: the trained adapter **never abstains** (0.0 on every split, vs the
base's calibrated 0.27–0.31), and the lost control rows are bedrock knowledge
answered confidently wrong ("boiling point = 93", "Buzz Aldrin first on the
Moon", "5 continents"). No-replay training converted calibrated abstention
into confident hallucination — the exact failure §2.2/§2.3 predicted. Replay's
necessity is now a **measurement**, not an assumption; cycle 2 with
`facts-w1-train` mixed back in is the controlled experiment (#1734).

Rulings this session forced, now normative:

1. **Measurement serves the training-time base.** The cycle-1 adapter's
   `base_model_name_or_path` is the **4-bit** quant it was trained against, so
   the honest champion is *that* base — one process, one load, adapter
   **disabled** (`peft disable_adapter()`) for the champion and enabled for the
   challenger. Serving the LoRA over the fp16 base (what `serve/vllm` would do)
   measures the adapter against a model it was never fitted to; the delta would
   partly be quantization, not training. vLLM multi-LoRA remains the
   *production* serving story (§2.5); for *measurement* the shim layout is the
   reference. Corollary: both sides must always come from the same server in
   the same session — never mix harnesses.
2. **Measurement validity is a governed gate, not a hope** (#1720, merged).
   `eval.closedbook` emits `{split}_error_rate` (endpoint errors + unparsed
   judge answers — the latter previously counted in the denominator and in no
   bucket); `measurement-valid` sits between the measures and the verdicts, and
   the verdict legs depend on the gate. A judge outage is a measurement fault
   and must block as one — before this, it read as catastrophic forgetting on a
   green leg (demonstrated: a dead judge produced a fabricated 90-point
   regression). Proven live in both directions in one session: run 1 blocked on
   a degraded measurement, run 2 passed cleanly and then blocked on the real
   regression.
3. **The validity bar is a declared tolerance** (#1733). One off-vocabulary
   judge answer in 706 rows blocked an otherwise perfect measurement; real
   judges emit occasional garbage, so hard zero cries wolf at scale.
   `max_error_rate` defaults to 0.0 (strict) and loosening it is a per-run,
   on-the-record decision — never a silent constant.
4. **The gate is directional; the statistic is paired.** At n=103 control, one
   row is 0.97 accuracy points and the unpaired noise band dwarfs the 0.02
   tolerance. The honest read is the paired McNemar off the per-row labels in
   the two `*_answers.jsonl` artifacts; the gate stops the pipeline, the paired
   test earns the claim. Report forgetting numbers with their significance, and
   treat a gate pass at this n as "no detected forgetting", never "none".

## Amendment 3 (2026-08-27) — replay works; calibration does not come back

Cycle 2 trained from cl-cycle1 on window 2 **with 20% replay of window 1**
(`dataset/replay`, ratio 0.2009 actual — 438 rows = 350 new + 88 replayed) and
was measured against cl-cycle1 on **both** windows, one server session, two
adapters on one 4-bit base, matched hyperparameters. §2.3's central claim —
anti-forgetting is a DATA operation — is now **measured, not assumed**.

### The contrast that carries the result

Same 103 control rows, same harness, both cycles:

| cycle | replay | control accuracy | lost / gained | paired McNemar |
|---|---|---|---|---|
| 1 (base → cl-cycle1) | **none** | 0.9515 → 0.8544 (−9.7 pts) | 12 / 2 | **p = 0.013** |
| 2 (cl-cycle1 → cl-cycle2) | **20%** | 0.8544 → 0.8447 (−0.97 pts) | **3 / 2** | **p = 1.000** |

Replay took catastrophic forgetting down to noise. And it is **not** retention
purchased by failing to learn: on window 2, paraphrase accuracy went
0.0686 → 0.5200, **+45 points, 82 gained / 3 lost, p ≈ 5e-21**. Cycle 2 learned
a great deal and kept what cycle 1 knew.

### The finding that reframes the next problem

**Abstention never came back.** `abstain_rate` is **0.000 on every split for
both cycles**, against the base model's calibrated 0.27–0.31; control
hallucination sits at 15.5%. Replay repaired *what the model knows* and did
nothing for *how it behaves when it does not know*.

These are therefore **two distinct failures**, and the campaign has solved one:

* **knowledge loss** — a DATA problem, and replay is the answer (this
  amendment);
* **abstention collapse** — a CALIBRATION problem that survives replay
  untouched. It needs its own mechanism. Candidates worth evaluating, none
  adopted here: abstention-bearing examples in the mix (the training data
  contains no "I don't know" targets, which may be the whole story), a
  DPO/GRPO objective that rewards calibrated refusal, or a serving-side
  confidence gate. **Do not assume more replay fixes it — this cycle is the
  evidence that it does not.**

### Rulings

5. **Replay ratio 0.2 is validated as a default**, on this shape of data, at
   this scale. It is still an `$input` and still steerable; what changed is
   that the default is now backed by a measurement rather than a guess.
6. **A cycle's verdict needs BOTH windows.** Measuring only the new window
   hides forgetting; measuring only the old hides whether anything was learned.
   `no-regression` blocked both runs here — correctly, since cycle 2 *is* worse
   on window 1's own eval (−4.1 pts) — which is precisely the trade a human
   gate exists to put in front of a person rather than resolve automatically.
7. **Match the prior cycle's hyperparameters, and read them from the tracker
   rather than choosing them.** The first attempt at cycle 2 set `packing`
   (default `True`) where cycle 1 used `False`: 438 rows collapsed into **14**
   packed sequences, making `Num Epochs = 200` and each step ~35s — a 4-hour,
   heavily-overfitting run whose delta would have confounded "replay works"
   with "different training setup". Cycle 1's real config lives in MLflow
   (`continual-learning` experiment, run `cl-cycle1`); with it matched, cycle 2
   trained in 307s against cycle 1's 327s. A continual-learning comparison is
   only as good as the constancy of everything except the variable under test.
8. **Aggregate accuracy hides churn; report the paired counts.** w1 paraphrase
   moved −6.5 points, which looks like mild drift until the pairing shows **34
   lost and 23 gained** underneath it. The headline number and the discordant
   pair counts answer different questions, and a forgetting claim needs both.

## Amendment 4 (2026-08-27) — abstention is a DATA problem too; the mix had no "I don't know" targets

Amendment 3 closed knowledge loss with replay and left one failure open: after
any training, `abstain_rate` fell to **0.000** on every split against the base
model's 0.27–0.31, and the model hallucinated where it had previously refused.
That is now measured and, on this evidence, solved by the same class of fix.

### The controlled arm

Same init adapter (`cl-cycle1`), same window-2 data, same 20% replay, same
hyperparameters. **The only difference: 92 synthetic unanswerable questions
whose target is "I don't know", 17.4% of a 530-row mix.** Both adapters served
from one base in one session and measured on one harness.

**Unanswerable questions (n=40, held out, never trained on — abstaining IS the
correct behaviour):**

| | cl-cycle2 (no abstention rows) | cl-abstain (17.4%) |
|---|---|---|
| hallucinated | **100%** | **22.5%** (−77.5 pts) |
| refused (abstain + correct) | **0%** | **77.5%** |

Paired McNemar **p = 6.1e-05**, 15 gained / 0 lost.

**Known facts (n=103) — the cost, which is what could have made this worthless:**

| | cl-cycle2 | cl-abstain |
|---|---|---|
| correct | 0.845 | 0.825 (−1.9 pts) |
| over-abstained | 0.000 | **0.010** |

**p = 0.727** — not significant. Refusal was not bought by becoming timid.

### Rulings

9. **Abstention collapse is a DATA problem, not an objective problem.** The
   training mix contained **no abstention targets at all**: every row was a
   question with a confident answer, and SFT on 100% "here is the answer" data
   teaches *always answer* as surely as it teaches any individual fact. Before
   reaching for a DPO/GRPO objective that rewards calibrated refusal (the
   heavier lane, still available), put "I don't know" rows in the mix. This
   amendment is the evidence that the cheap fix works.
10. **Unanswerable rows must be SYNTHETIC and disjoint from every window.**
    They are generated as plausible news-shaped questions about invented events
    — never held-out real facts, which would teach the model to refuse
    something a later cycle is supposed to learn. They carry
    `kind: unanswerable`, `answer_type: abstain`, and a held-out slice is kept
    to MEASURE with (abstaining on it is scored correct by the existing judge,
    so no new harness is needed).
11. **Report the over-abstention cost alongside the gain, always.** A model
    that refuses everything scores perfectly on unanswerables and is useless.
    The known-facts arm is not optional context — it is the other half of the
    claim, and 17.4% is validated only because that arm stayed flat.
12. **⚠ Checkpoint keys must be RUN-SCOPED.** The first attempt at this arm
    reported 400 steps in **13 seconds** with `train_loss` 0.0003.
    `train_entry` calls `checkpoint_sync.pull_checkpoints_if_any(out_dir)`
    before training, and `s3://…/campaign/checkpoints/**adapter/**` is a
    GENERIC key: it pulled a different run's checkpoints into a brand-new
    output directory and resumed from step 400, running one step. Nothing
    warned. Had the timing not been implausible, an adapter that never trained
    would have been measured and written up as a result. A continual-learning
    campaign is a chain of runs that all look alike to a shared key — this is
    the failure mode that silently fabricates experiments, and the fix is to
    scope the checkpoint prefix per run. Filed as #1749; until it lands,
    experimental runs must unset `CAIRN_CHECKPOINT_BUCKET`/`_PREFIX`.

## Amendment 5 (2026-08-27) — the judge was lenient; what survives, and one regression it was hiding

The first human validation of the closed-book judge (#1656) found a rubric
defect (#1752, fixed): *"numbers within 1% or rounding"* applied to every
number, so a model answering **0.9175** for a gold of **0.9217** scored
**correct**. Against 66 human-labelled rows the judge showed **16 lenient
errors and 0 harsh** (Cohen's kappa **0.441**) — every one a near-miss rate.
With the corrected rubric the same rows re-judge at **100% agreement, kappa
1.000, 0 lenient / 0 harsh**.

All per-row answers from cycles 1–2 were preserved, so the record was
**re-derived rather than patched** — judge calls only, no retraining, no GPU.

### What survives, measured not assumed

| claim | old | corrected |
|---|---|---|
| **Replay kills forgetting** (Am. 3): control | 0.8544 → 0.8447, p=1.000 | **identical**, p=1.000 |
| **Learning on the new window** (w2 paraphrase) | +45.14 pts, p=5e-21 | **+44.57 pts, p=1e-20** |
| **Abstention rows kill hallucination** (Am. 4) | 100% → 22.5% halluc | **100% → 22.5%**, known facts unchanged |

The rubric fix touched **zero** control rows. Amendments 3 and 4 stand.

### 13. A lenient judge hid a real regression

Correcting the rubric did not only deflate numbers — it **revealed** one:

| w1 paraphrase | delta | McNemar |
|---|---|---|
| lenient judge | −6.51 pts | p=0.185 — not significant |
| **corrected** | **−10.65 pts** | **p=0.011 — significant** (32 lost / 14 gained) |

So **replay at 20% preserved the bedrock control split but did NOT preserve
window 1's own paraphrase knowledge.** Amendment 3's "replay works" is hereby
narrowed: it protects what the control split measures; window-specific
knowledge still degrades measurably. A future cycle should either raise the
replay ratio or replay *paraphrase* rows, not only train rows — and measure the
previous window's paraphrase split explicitly, which is now the sharpest
forgetting signal available.

### 14. Eval-split accuracies were largely artifact

w1 eval: champion 0.2192 → **0.0685**, challenger 0.1781 → **0.0000**. Nearly
all of the reported w1 eval accuracy was near-miss rates scored as correct. The
w2 split, only 14.7% numeric against w1's 29.3%, barely moved. **Absolute
closed-book accuracy on numeric-heavy fact sets was not trustworthy before
#1752**; deltas were more robust than levels because both arms carried the same
bias, which is why the headline comparisons survived.

### 15. Validate a substitute judge before swapping it in

The re-grade ran out of DeepSeek balance partway. The remaining rows were graded
by `openai/gpt-4o-mini`, but **only after checking it against the same 66 human
labels** — 100% agreement, kappa 1.000, 0 lenient, matching deepseek-v4-pro
exactly on the corrected rubric. Swapping judges mid-record is a comparability
change (`prompt_hash`/`judge_model` exist to make that visible); it is
acceptable when both arms of a comparison share one judge AND that judge has
been validated on the same labels. Do not skip the second half.

### 16. An ad-hoc measurement script needs the pipeline's validity gate

The first calibration re-grade reported control accuracy falling **61 points**.
It was rate-limit failures: 103 and 110 of 143 rows returned `error`, and error
rows are not `correct`. This is exactly the #1720 failure mode the
`measurement-valid` gate was built to stop — and the script had no such gate,
so it produced a confident fabricated number that was one step from being
written down. **Any script that computes a metric outside the pipeline must
carry the pipeline's validity check with it.**
