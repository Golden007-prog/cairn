# ADR-0102 — The training experience start to end: dataset roles and intake verdict, three configuration tiers, live metrics over the job channel, measured judge trust, harnesses, catalog and export, results as the home

- **Status:** ACCEPTED (owner walked thirteen decisions one by one, 2026-09-05)
- **Date:** 2026-09-05
- **Companion:** `docs/training-experience-design-2026-09-04.md` (the long form: research findings, decisions per element, console journey, wire-vs-build, tooling, slices) and `docs/audits/training-experience-research-2026-09-04/` (eight research reports A–F plus G, the cross-verification ledger)
- **Extends:** ADR-0080 (dataset registry), ADR-0043 (remote-job channel), ADR-0068/0070/0071 (config scoping and presets), ADR-0095 (continual learning, judge scores as untrusted metrics), ADR-0099 (services required per pipeline), ADR-0101 (one start model). Supersedes the "drop NATS from the remote-job path" decision in #553 for remote **training** jobs only.
- **Epic:** training-experience epic (filed with this ADR); cross-linked to #1981 (the training loop)

## Context

The owner asked for the training pipeline architecture and console UX from start to end across five elements — how datasets are provided, how configuration is provided in three tiers (platform-suggested · advanced · mid-training), how eval and golden sets are provided or computed, how live metrics, progress and lineage are shown, and how the model is evaluated and benchmarked — plus export of every artifact, a built-in open-weight model catalog fed from HF or S3, and the tooling a platform-intelligence layer needs beyond a trainer, MLflow/W&B and Optuna.

The research (A–G) established, against primary sources, that: no fine-tuning platform validates data on intake beyond format, auto-splits, checks contamination, pins the eval-set version into the run, or runs a forgetting check — cairn already has all of these; a platform that suggests configuration from the data exists nowhere as a documented feature; mid-training suggestion is empty industry-wide because the Hugging Face callback contract is read-only, so the honest shape is stop → checkpoint → clone with a warm start; and the mature, alive tools to wire are Unsloth/TRL, MLflow, Optuna, lm-eval-harness, Inspect AI and Unsloth's GGUF export.

## Decisions

**D1 — Dataset roles, frozen splits, and an intake verdict.** Every registry version carries `role ∈ {training, validation, test, golden}` and a declared `schema` with a `column_mapping`. `golden` is human-rated only and exists to benchmark judges. A split produces frozen child versions with lineage; items never move; re-splitting creates new children; the default is declared and stratified, never a random tail. Intake is two-phase: a browser-side check, then the `clean` flow runs automatically (exact and near-duplicate dedup, the PII cascade, decontamination against every golden and test version in the workspace, the token-length profile with the target tokenizer, declared expectations) and writes a verdict `admitted | degraded | blocked | invalid_format` with a structured report on the version. **The launch door refuses `blocked` and `invalid_format`; `degraded` runs with its reasons shown.** Over-length rows are counted and dropped or refused, never silently truncated. Seven intake channels (upload, object-store URI by reference, HF Hub id, database table via `ingest/pull`, Delta/Parquet as files, API via `ingest/api`, watch source) share one **Add dataset** door.

**D2 — Configuration in three tiers on one contract.** `TrainSpec` is the single contract and every training pipeline exposes it fully at its input layer. *Tier one:* the run dialog opens on **Suggested** cards — a portfolio of four to six configurations from the prior-region recommender (`docs/training-config-warm-start.md`), each with a reason, a confidence and a cost estimate; data-dependent rules (method from schema, epochs from row band, `max_seq_len` from p95 tokens, rank from size band, learning rate as a region, method forced by VRAM fit) are a table the recommender reads; the portfolio is injected into an **Optuna** study with `enqueue_trial`. *Tier two:* one **Simple/Advanced** toggle; Advanced shows the full spec with **environment-owned fields locked and labelled**, not hidden; preview of the effective configuration; YAML/JSON import and export; clone from a run; a **configuration diff** between runs; an Optuna study as a first-class object on a series, run as the bounded `loop:` leg. *Tier three:* early kill on curves through the Optuna pruner (stop only); a **divergence guard that may stop a run on NaN or a loss spike on its own and may never start the retry**; a suggest-next proposal that clones with a warm start via `init_adapter_uri`; every clone records `parent_run` and the field diff so a chain reads as a schedule. The effective configuration after every default is written on the run, exported with the artifact, and embedded in the model card. **The tier-two history lives in cairn's own Postgres**: a `training_runs` table, one row per run (effective spec, dataset version and meta-features, portfolio key, curve summary, eval scores, cost, outcome, proxy flag, parent), with Optuna's study storage on the same database; MLflow remains the artifact and full-curve mirror and is never the optimiser's source of truth, because it is external and optional per deployment. Not built: in-place hyperparameter mutation, curve extrapolation, a surrogate.

**D3 — Measured judge trust.** Every judge gets an agreement panel against the workspace's golden sets: Kendall's τ, MAE, n. A judge with no golden evaluation is untrusted by default. Below the workspace threshold (default τ ≥ 0.5 with n ≥ 30) a judge's scores are `untrusted_metric`; above it they may contribute to a promotion gate and never satisfy one alone. The holdout door refuses a training version that intersects any golden or test version, exact then near-duplicate, and the run record carries every eval-set version hash.

**D4 — Live metrics over the job channel; the store is the log.** A `TrainerCallback` in the training pack emits run events — `metrics` at `logging_steps`, `system` every 15 s, `progress` with stage/ETA/cost, `checkpoint`, `divergence` — for every provider. Remote providers use the existing ADR-0043 job channel (events up, control down) with two backends behind `OBS_JOB_CHANNEL`: **NATS JetStream in the full profile, declared as a required service by the remote training operators so the launch door refuses rather than auto-starts it (ADR-0099)**, and an HTTPS long-poll backend without a broker. In every case the run store is the durable event log and the transport only a wake-up: the callback keeps a local append spool and every event carries `(run, seq)`. Control-down gains `checkpoint-and-stop`. The console gets a **Training** tab on the run (loss and eval loss, learning rate and gradient norm, progress, checkpoints with clone-from-here and serve, a capped live log tail, the effective config with diff), a series overlay, and a lineage graph; the lineage record gains base model and revision, eval-set hashes, parent run, image and commit, and exports into the HF model-card fields (`base_model`, `base_model_relation`, `datasets`, `license`, `model-index`). MLflow stays the durable tracker. Cairn-light is unaffected; it carries no ML modules.

**D5 — Catalog, evaluation, export.** A curated model catalog shipped as data in a pack — the under-15B families Qwen, Llama, Mistral, Gemma, DeepSeek, GLM in v1 — with identity, licence and `gated` flag, context, the VRAM-by-method table, recommended method and source (`hf` or `s3://` via the artifact store); **any HF id** as the advanced escape hatch; a gated model needs the user's HF token and an explicit `accept_license: true` recorded in the audit chain. After training: **both** lm-eval-harness (classic benchmarks, decontamination on, YAML tasks as pack data) and Inspect AI (judged and agentic, `eval_set()` resume on stable row ids); the forgetting gate automatic on every candidate; the judge scorecard with its agreement badge; pairwise against the champion on batch inference; evaluation cost as a budget that fails loudly, not an estimate. Export from the Artifacts tab: adapter, merged 16-bit, **GGUF through Unsloth's `save_pretrained_gguf` in the merge operator**, Ollama Modelfile, vLLM-ready directory, and the **run bundle** (effective config, metrics, eval results, lineage, model card).

**D6 — Results is the home; the nav does not change.** Runs › Results gains a **Models lens** beside Runs and Data products (registered versions, aliases, lineage graph, export, serve) and headline metrics on every row (eval score, forgetting verdict, judge-agreement badge, cost). No Models nav item: a registered model is one kind of result, and the platform stays kind-neutral rather than reading as ModelOps. Live metrics live on the run's Training tab; cross-run comparison on Series.

**D7 — Activity and metrics are generic, declared per operator (owner add, 2026-09-05).** The pipeline page is the **Activity page**, live from the run-event stream rather than polling: each step row shows status, the stage inside the step, percent, ETA, cost so far, what it is waiting on, and a per-step log tail. The pipeline gains a **Metrics tab**, one section per step — Parameters (the step's effective inputs and config after defaults, which for a training step are the hyperparameters), Series (charted), Scalars with gate verdicts, Artifacts, Cost, and the clone chain. **Nothing on either page knows the word "training".** The vocabulary comes from the **operator reporting contract, ADR-0103**: `reports` is a required class attribute on `Operation` beside `risk` (stages, series with units, scalars; `NONE` must be explicit), mirrored in `pack.toml` and compared by the manifest gates; the context's typed reporter refuses undeclared names in tests and drops them with a warning in a job; the executor records every step's effective inputs and config before `run`, so preview, import/export, clone and diff work for any operator without the operator writing a line. The run's Training tab is the same renderer applied to one run. A readiness pipeline, a red-team campaign, a batch-inference job and a training pipeline all render through the same two surfaces.

## Verification (verify-upstream)

| Check | Result |
|---|---|
| A · LangGraph | not applicable |
| B · workflow engine | the ranking loop is the bounded `loop:` leg (ADR-0084 D7); the job channel is ADR-0043 slice 2, parked by #553, with NATS and in-memory implementations already written; the SSE bridge already states "the store is the durable event log; the bus is a wake-up" |
| C · Python ecosystem | wire Optuna, lm-eval-harness, Inspect AI, Unsloth GGUF, HF `tokenizers`, `nats-py` (existing optional extra); MLflow, Presidio, DuckDB, datasketch already present |
| D · this codebase | dataset registry + `dataset_meta`; `SplitOp`; `dedup.exact`, `dedup_minhash`, `decontam.apply`, `pii.redact`, `dataset/profile`, `data_expectations`; `TrainSpec`; presets + config scoping + workspace variables; judges + eval ledger + `eval/forgetting`; run-event store + SSE stream; `lineage/record`; `model/card`, `model/merge` (GGUF stub); `serve/vllm`, `infer/batch`; artifact store S3 backend |

**Build:** roles + schema + verdict + holdout door; Add dataset door; full-spec exposure + tiers UX + diff; recommender rules + portfolio; the callback and the two job-channel backends + `checkpoint-and-stop`; Training tab, series overlay, lineage graph; agreement panel + trust rule; catalog data pack + licence acceptance; harness operator packs + presets; GGUF wiring + run bundle; Results lenses + metric columns. **Not built:** a trainer, a tracker, an optimiser, a harness, a surrogate, curve extrapolation, in-place mutation, a Models page.

## Slices (the training-experience epic)

| Slice | Content | Wave | Existing |
|---|---|---|---|
| T1 | roles + schema + column mapping; frozen child-version splits; the holdout door | A | absorbs #1990 |
| T2 | intake verdict: minhash into `clean.yaml`, automatic token profile, verdict + report, door reads it | A | |
| T3 | Add dataset door with seven channels; Parquet in the picker | A | |
| T4 | full `TrainSpec` at every pipeline's input layer; Simple/Advanced with locked fields; preview, import/export, clone-from-run, config diff | A | |
| T5 | judge agreement panel + trust rule | A | extends #1467, ADR-0095 |
| T6 | `TrainerCallback` → run events over the job channel; NATS declared service + HTTPS long-poll; spool + `(run, seq)`; `checkpoint-and-stop` | B | ADR-0043, #553 |
| T7 | Training tab, series overlay, lineage graph | B | |
| T8 | catalog data pack + picker with GPU fit + `accept_license`; S3 base-model source | B | |
| T15 | `training_runs` history table in cairn's own Postgres — one row per run: effective spec, dataset version + meta-features, portfolio key, curve summary, scores, cost, outcome, proxy flag, parent — with Optuna's study storage on the same database; MLflow stays the mirror, never the optimiser's source of truth | B (before T9) | owner add, 2026-09-05 |
| T9 | recommender + portfolio → Optuna `enqueue_trial`; Suggested cards | B | #2002 |
| T10 | proxy-run ranking as the `loop:` leg with pruning | B | #2003 |
| T11 | clone-with-warm-start proposal, divergence guard, parent chain, schedule view | C | |
| T12 | lm-eval-harness + Inspect AI packs with task presets; automatic forgetting gate; pairwise vs champion | C | near #1994–#1996 |
| T17 | durable byte backend for non-checkpoint artifacts (#1395): eval results, reports and the export bundle survive the box; T12/T13 depend on it — the one gap the 2026-09-05 stack review found that was not already here | C (before T13) | #1395 |
| T13 | Export chain (GGUF, Modelfile, vLLM dir, run bundle); Results › Models lens + metric columns | C | |
| T14 | eval rows mined from failures reference the trace; dated rows; synthetic eval with expected facts | C | #1989, #1993 |

Wave A needs no GPU. **Compute (owner, 2026-09-05):** Metaflow already runs on Kubernetes (ADR-0089); distributed training, when a model no longer fits one GPU, is KubeRay + Ray Tune on that cluster (ADR-0081's scale rung) — not Kueue, Slurm or FSDP first. The external stack review's other recommendations were verified against the code and either already exist (DuckDB, Polars, HF datasets, Prometheus/Grafana profile, S3 checkpoints, the eval ledger) or were decided otherwise on record (MLflow as mirror, lm-eval + Inspect, no lakeFS, ranking before a surrogate).

## Consequences

- The data-readiness pack's checks become the intake, not a pipeline a user must know to run; the verdict is state the door reads.
- Two tiers of configuration UX rest on scoping rules that already exist; the third tier is proposals and stops, never spend.
- NATS returns to the remote-job path for training only, as a declared service; the #553 decision stands for everything else, and cairn-light is untouched.
- Judges become instruments with a calibration number; an uncalibrated judge cannot move a promotion.
- The nav stays five items; the model is a result.

## Alternatives rejected

- **A Models nav item** — tilts the platform toward ModelOps; Results already lists what runs produce.
- **NATS required with no HTTPS fallback** — tighter coupling than any other cairn service has.
- **HTTPS only, job channel left dormant** — forgoes graceful checkpoint-and-stop, which tier three needs.
- **Auto-retry within the cost cap** — breaks the rule that nothing spends unasked.
- **A judge that can promote alone above threshold** — the one path where a judge failure ships a model.
- **Syne Tune or Ray Tune as the optimiser** — alive, but heavier; Optuna's `enqueue_trial` is the portfolio and its storage the trial record. Syne Tune stays the alternative if cross-task transfer becomes the priority.
- **lighteval, HELM** — pivoted to Inspect; maintenance mode.
