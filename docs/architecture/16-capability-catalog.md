# Capability catalog — what cairn can run (HEAD `81d876ae`, 2026-09-10)

Produced by the `/catalog` skill. Method: every `pack.toml`, `pipelines/*.yaml`, `flows/*`,
`skills/*.md`, operator `pyproject.toml` entry-point group and `src/cairn/cli/main.py` was parsed
(YAML/TOML, not grep), then cross-checked against the live `cairn pipeline ls`, `cairn.lock`,
root `pyproject.toml` registration, tests and README/ADR claims. Tags: **OBSERVED** (in
source) · **DOCUMENTED** (docs say) · **INFERRED** · **CONFLICT**. Read-only; nothing was changed.

## Summary numbers (OBSERVED)
| Unit | Count | Note |
|---|---|---|
| Domain packs on disk | 9 dirs (8 `domain`, 1 knowledge, `security_kb`) | all 9 registered in `cairn.packs` (`redteam`, `redteam_kb` retired in #2413) |
| Pipelines | **31 files** / **28 installed** | `cairn pipeline ls` lists 28; the 3 unregistered `redteam/*` files were since retired (#2413) |
| Flows (`kind: data`/`ml`, YAML) | 12 (10 data, 2 ml) | `training` 9, `compress` 1, `dataset_readiness` 1, `serving` 1 |
| Agentic templates (`flows/*.md`) | 27 (25 `state_machine`, 2 `prompt`) | `dataset_readiness` 9, `training` 8, `incident` 5, one each: compress, compute, eval, security_triage, serving |
| Skills (legacy trigger bindings) | 3 | `incident` 2, `dataset_readiness` 1 |
| Operation bindings (`operations/*.yaml`) | 32 | `training` 30, `dataset_readiness` 2 |
| Operator packs | 37 on disk (14 builtins, 23 official), all 37 in the uv workspace | `redteam` joined the workspace and the root dependencies in #2289; `rag_parse` retired in #2413; `remote` (#2428) and `secrets_infisical` (hello-cairn WP2) moved to builtins; `train_local` joined (ADR-0127 D4) |
| Operators (entry points) | **113** registered / **113** discoverable in a normal install | the 7 `redteam/*` install since #2289; `rag/parse` retired in #2413; `train/lora`, `train/eval-loss` (ADR-0127 D4) |
| Flow stage kernels | 34 names across 9 packs | `dataset_checks` 14, `training` 6, `dataset_synth` 2, `ingest` 2, `dataflow` 2 (noop), `eval` 1, `serving` 1, `data_expectations` 1, `train_local` 1 (`train.lora`) |
| Dataflow engines | 3 entry points (`builtin`, `metaflow`, `skypilot`) + SSH variants inside `operator_dataflow` | `packages/cairn-operator-dataflow/pyproject.toml:39` |
| CLI | 13 top-level verbs + 17 command groups (≈ 88 subcommands) | `src/cairn/cli/main.py:61-130` |
| API doors that start or steer work | 12 | listed under Invocation |
| Console scripts | `cairn`, `cairn-server`, `cairn-worker`, `cairn-admin`, `cairn-dataflow-run`, `cairn-train-local` | root + 2 packs |
| **Runnable units total** | 31 pipelines + 12 flows + 27 templates + 114 operators = **184** (+ 34 stage kernels runnable only inside a flow) | |

## Capability catalog by domain

### Training (pack `training`, `cairn/packs/training/`; code `cairn/operators/official/training/`)
| Capability | Kind | Pack | Entry point | Implementation | Composes | Tag |
|---|---|---|---|---|---|---|
| `training/verifiable-finetune` | pipeline (10 legs, budget 50, approvers ml-leads) | training | `pipelines/verifiable-finetune.yaml` | bridge | flows facts-synth → facts-prep · closedbook-eval (baseline) → gate novelty → flow finetune → gate serve-adapter (HITL) → closedbook-eval (post) → task `eval/forgetting` → gates no-forgetting, promote-model | OBSERVED; tests 3 |
| `training/train-only` | pipeline (1 leg, budget 20) | training | `pipelines/train-only.yaml` | bridge | flow finetune | OBSERVED; tests 4 |
| `training/train-managed` | pipeline (9 legs, budget 100) | training | `pipelines/train-managed.yaml` | bridge | task `compute/estimate` → gates cost-in-budget, authorize-spend → tasks `train/runpod|vast|modal|stub` (routed by `when`) → gate promote-adapter → task `train/teardown` | OBSERVED; tests 1 |
| `training/e2e-finetune` | pipeline (3 legs, budget 300) | training | `pipelines/e2e-finetune.yaml` | bridge | flow readiness → flow finetune → gate promote-model | OBSERVED; tests 9 |
| `training/e2e-fullstack` | pipeline (7 legs, budget 100) | training | `pipelines/e2e-fullstack.yaml` | bridge | flow data-prep → gate → flow data-curate → gate → flow finetune → gate → template e2e-report | OBSERVED; tests 3; SDK twin `examples/e2e_fullstack_sdk.py` |
| `training/continual-learning` | pipeline (loop `cycle`, budget 300) | training | `pipelines/continual-learning.yaml` | bridge | loop: flow readiness → gate enough-signal → closedbook-eval → task `dataset/replay` → flow finetune → gate serve-challenger → closedbook-eval → gate measurement-valid → tasks `eval/delta`, `eval/forgetting` → gates no-regression, no-forgetting, promote-adapter | OBSERVED; tests 8; ADR-0095 |
| `training/continual-learning-managed` | pipeline (6 top-level legs incl. loop, budget 300) | training | `pipelines/continual-learning-managed.yaml` | bridge | task `compute/estimate` → gate authorize-compute → task `compute/acquire` → loop (as above with tasks `serve/start`) → task `serve/stop` → task `compute/release` | OBSERVED; tests 2; ADR-0097 |
| `training/dpo-pairs` | pipeline (1 leg) | training | `pipelines/dpo-pairs.yaml` | bridge | flow dpo-pairs | OBSERVED; tests 2 |
| `training/dpo-train` | pipeline (1 leg, budget 20) | training | `pipelines/dpo-train.yaml` | bridge | flow dpo-align (`train.dpo`) | OBSERVED; tests 2 |
| `training/facts-window` | pipeline (2 legs) | training | `pipelines/facts-window.yaml` | bridge | flows facts-synth → facts-prep | OBSERVED; tests 1 |
| `training/proxy-rank` | pipeline (loop `rank`, budget 15) | training | `pipelines/proxy-rank.yaml` | bridge | task `train/proxy-trial` → flow finetune → task `train/proxy-trial` (Optuna, ADR-0102 D2) | OBSERVED; tests 1 |
| `training/closedbook-measure` | pipeline (1 leg) | training | `pipelines/closedbook-measure.yaml` | bridge | flow closedbook-eval | OBSERVED; tests 1 |
| `training/closedbook-compare` | pipeline (7 legs, budget 20) | training | `pipelines/closedbook-compare.yaml` | bridge | closedbook-eval ×2 → gate measurement-valid → tasks `eval/delta`, `eval/forgetting` → gates no-regression, no-forgetting | OBSERVED; tests 1 (`tests/core/test_closedbook_measurement_validity.py:45,71`) |
| `training/finetune` | flow `kind: ml` | training | `flows/finetune.yaml` | stages `sample.fidelity → train.qlora → eval.checkpoints → package.adapter` (`operator_training.flow_stages`, entry group `cairn.flow_stages`, `cairn/operators/official/training/pyproject.toml:60-71`) | used by 6 pipelines | OBSERVED |
| `training/dpo-align` | flow `kind: ml` | training | `flows/dpo-align.yaml` | stage `train.dpo` | dpo-train | OBSERVED |
| `training/{readiness, data-prep, data-curate, dpo-pairs, facts-prep, facts-synth, closedbook-eval}` | flows `kind: data` | training | `flows/*.yaml` | stages from `operator_dataset_checks`, `operator_dataset_synth` (`facts.collect`, `qa.generate`), `operator_eval` (`eval.closedbook`) | pipelines above | OBSERVED (D5 copies of dataset-readiness/compress stage lists) |
| `training/finetune` (agentic) | template `state_machine` (16 operation bindings) | training | `flows/finetune.md` | LangGraph via `run_template`; `subgraph_node dataset-readiness/check` (cross-pack); operations `run-cost-estimate/gate/audit`, `run-train`, `run-cost-actual`, `run-benchmark`, `run-model-gate`, `run-promote`… | `[[demo.finetune]]` ×3 tiers; `[eval.finetune]` recipe | OBSERVED; **CONFLICT** with ADR-0084 D5 (cross-pack ref still present) |
| `training/finetune-remote`, `train`, `e2e-fullstack`, `e2e-report`, `composed-hitl-smoke`, `train-remote-smoke`, `train-remote-hitl-smoke` | templates | training | `flows/*.md` | LangGraph | `finetune-remote` also subgraphs `dataset-readiness/check`; smoke templates are test fixtures shipped in the pack (INFERRED from names) | OBSERVED |
| `train/{stub,local,modal,kaggle,runpod,vast}` | operators (capability `train.run`) | cairn-operator-training | `pyproject.toml:38-44` | `operators/{stub,local,modal,kaggle,runpod,vast}.py`; paid providers `risk=paid_compute`, `executors=[remote_job,in_process]` | bound by `operations/run-train*.yaml`; task legs in train-managed | OBSERVED |
| `train/stage-dataset`, `train/teardown`, `train/proxy-trial`, `compress/distill` | operators | cairn-operator-training | `pyproject.toml:43-56` | | `compress/distill` lives here (no cross-pack imports) — used by `compress/distill` pipeline | OBSERVED; naming note |
| `cairn-train-local` | console script | cairn-operator-training | `[project.scripts]` | local training entry | | OBSERVED |
| Core training state | pack capability | core | `src/cairn/core/storage/training_runs.py`, `operator/pins.py`, `mid_training.py`, `recommender.py`, `models_catalog.py`, `model_license_gate.py` | | | OBSERVED |

### Dataset preparation & readiness (pack `dataset-readiness`, `cairn/packs/dataset_readiness/`; code `cairn-operator-dataset-checks`, `cairn-operator-ingest`, `cairn-operator-data-profile`, `cairn-operator-data-expectations`, `cairn-operator-dataset-synth`)
| Capability | Kind | Pack | Entry point | Implementation | Composes | Tag |
|---|---|---|---|---|---|---|
| `dataset_readiness/import` | pipeline (1 leg, budget 1) | dataset-readiness | `pipelines/import.yaml` | bridge | flow clean | OBSERVED; tests 4 |
| `dataset_readiness/readiness` | pipeline (template + loop ≤3, `on_exhausted: gate`, nightly `on: cron`) | dataset-readiness | `pipelines/readiness.yaml` | bridge | template check → loop remediate (flow clean → template check) | OBSERVED; tests 18 |
| `dataset_readiness/pull-and-admit` | pipeline (3 legs) | dataset-readiness | `pipelines/pull-and-admit.yaml` | bridge | gate approve-pull → task `ingest/pull` → flow clean | OBSERVED; tests 1 (`tests/packs/dataset_readiness/test_pull_and_admit.py`) |
| `dataset_readiness/api-and-admit` | pipeline (3 legs) | dataset-readiness | `pipelines/api-and-admit.yaml` | bridge | gate approve-pull → task `ingest/api` → flow clean | OBSERVED; tests 1 (`tests/packs/dataset_readiness/test_api_and_admit.py`) |
| `dataset_readiness/watch-source` | pipeline (7 legs) | dataset-readiness | `pipelines/watch-source.yaml` | bridge | task `ingest/watch` → task `ingest/pull` → flow clean → task `ingest/watch` (commit) → task `monitor/propose` → task `monitor/dispatch` | fired by declared `watch` triggers (`engine/triggers_grammar.py:624`) | OBSERVED; tests 1 |
| `dataset_readiness/clean` | flow (no `kind:` → defaults `data`, `dataflow/defs.py:77`) | dataset-readiness | `flows/clean.yaml` | stages `ingest.normalize → dedup.exact → dedup.near → decontam.apply → pii.redact → expectations.check → profile.tokens → export.jsonl` | 5 pipelines | OBSERVED |
| `dataset_readiness/check` | template `state_machine` (3 operations) | dataset-readiness | `flows/check.md` | LangGraph; operators `dataset/*`; RAG role `data_governance` | subgraphed by training templates; `[[demo.check]]` | OBSERVED |
| `clean`, `prep`, `decontaminate`, `promote`, `watch` | templates | dataset-readiness | `flows/*.md` | LangGraph | `[[demo.prep]]`; `promote` → `artifact/promote` | OBSERVED; `clean.md` and `clean.yaml` share a name — the pipeline's `flow: clean` resolves the YAML (`leg_kind_of` = flow) |
| skill `dataset-readiness` | skill (triggers `patterns`, `manual`) → template check | dataset-readiness | `skills/*.md` | `engine/skill_engine.py` | | OBSERVED (legacy path) |
| `dataset/*` (21 operators) | operators | cairn-operator-dataset-checks | `pyproject.toml` `cairn.operators` | `format, dedup, dedup_apply, pii_scan, scan_all, decontam, decontam_apply, profile, provenance, gate, prep_gate, prep_review, sample, anonymize, curate (paid_compute), split, replay, capture_to_{sft,pairs,rewards}, publish_fingerprints` | templates + task legs | OBSERVED |
| stages `ingest.normalize, dedup.exact, dedup.near, profile.tokens, pii.redact, export.parquet, export.jsonl, decontam.apply, decontam.scan, sample.rows, curate.score, split, scan.all, filter.rows` | stage kernels | cairn-operator-dataset-checks | `cairn.dataflow_stages`/`cairn.flow_stages` | | every data flow | OBSERVED |
| `ingest/pull`, `ingest/api`, `ingest/watch` (+ stages `ingest.sql`, `ingest.api`) | operators | cairn-operator-ingest | `pyproject.toml` | SQLAlchemy / httpx / watch markers | pull-and-admit, api-and-admit, watch-source | OBSERVED; pack tests **not in `testpaths`** |
| `data/profile`, `data/expectations` (+ stage `expectations.check`) | operators | cairn-operator-data-profile, cairn-operator-data-expectations | | | clean flow | OBSERVED |
| `dataset/synthesize`, `dataset/augment`, `dataset/synth-eval` (+ stages `facts.collect`, `qa.generate`) | operators (paid_compute) | cairn-operator-dataset-synth | | LLM via gateway | training facts-synth | OBSERVED; pack tests not in `testpaths` |
| Dataset registry, roles, intake/holdout doors | pack capability | core | `storage/artifacts.py`, `dataset_roles.py`, `intake.py`, `holdout.py` | | consumed by `run_template` doors and `execute_leg` | OBSERVED |

### Serving (pack `serving`; code `cairn-operator-serving`, `cairn-operator-model-registry`, `cairn-operator-compute`)
| Capability | Kind | Pack | Entry point | Implementation | Composes | Tag |
|---|---|---|---|---|---|---|
| `serving/serve` | pipeline (2 legs) | serving | `pipelines/serve.yaml` | bridge | template serve → gate accept-endpoint (HITL) | OBSERVED; tests 4 |
| `serving/batch-inference` | pipeline (4 legs, budget 25) | serving | `pipelines/batch-inference.yaml` | bridge | task `cost/tokens` → gate cost (predicate) → flow batch-infer → task `cost/tokens` (actual) | OBSERVED; tests 3 |
| `serving/export-model` | pipeline (1 leg) | serving | `pipelines/export-model.yaml` | bridge | task `model/merge` (cairn-operator-model-registry) | OBSERVED; **tests 0** |
| `serving/batch-infer` | flow `data` | serving | `flows/batch-infer.yaml` | stages `ingest.normalize → infer.batch → export.jsonl` (`infer.batch` from cairn-operator-serving) | batch-inference | OBSERVED |
| `serving/serve` | template `state_machine` | serving | `flows/serve.md` | operators `serve/vllm`, `infer/bench` | serve pipeline | OBSERVED |
| `serve/vllm`, `serve/start` (paid_compute), `serve/stop`, `infer/bench`, `infer/batch` | operators | cairn-operator-serving | `pyproject.toml` | `vllm_serve.py:132`, `serve_lifecycle.py`, `infer_bench.py`, `infer_batch.py:197` | also task legs in continual-learning-managed | OBSERVED |
| `model/register`, `model/baseline`, `model/card`, `model/merge` | operators | cairn-operator-model-registry | | MLflow behind `[mlflow]` extra | training promote, export-model | OBSERVED |

### Evaluation & compression (packs `eval`, `compress`, `compute`; code `cairn-operator-eval`, `cairn-operator-eval-lm`, `cairn-operator-eval-judged`, `cairn-operator-compress`, `cairn-operator-compute`)
| Capability | Kind | Pack | Entry point | Implementation | Composes | Tag |
|---|---|---|---|---|---|---|
| `eval/score-a-model` | pipeline | eval | `pipelines/score-a-model.yaml` | template score → gate accept-regression | | OBSERVED |
| `compress/quantize` | pipeline | compress | `pipelines/quantize.yaml` | template quantize → gate accept-quality-delta | operator `compress/quantize` | OBSERVED; tests 2 |
| `compress/distill` | pipeline (budget 30) | compress | `pipelines/distill.yaml` | task `compress/distill` (lives in **cairn-operator-training**) → gate | | OBSERVED; ownership note |
| `compress/distill-compare` | pipeline (6 legs) | compress | `pipelines/distill-compare.yaml` | flow closedbook-eval ×2 → gate → task `eval/delta` → gates | | OBSERVED; tests 1 |
| `compress/closedbook-eval` | flow `data` | compress | `flows/closedbook-eval.yaml` | stages `ingest.normalize → eval.closedbook` | copy of training's flow (ADR-0084 D5) | OBSERVED |
| `compute/price-a-run` | pipeline | compute | `pipelines/price-a-run.yaml` | template cheapest-gpu | `compute/quote` | OBSERVED |
| `eval/judge`, `model/gate`, `eval/delta`, `eval/forgetting`, `model/eval`, `rag/eval`, `eval/benchmark`, `eval/subset`, `eval/langfuse` (+ stage `eval.closedbook`) | operators | cairn-operator-eval | | | task legs across training/compress | OBSERVED |
| `eval/lm-eval`; `eval/inspect`, `eval/pairwise` | operators (long-running, external_write) | cairn-operator-eval-lm; cairn-operator-eval-judged | | lm-eval / inspect-ai behind extras | ADR-0102 D5 | OBSERVED |
| `compress/stub`, `compress/quantize` | operators | cairn-operator-compress | | | quantize template | OBSERVED |
| `compute/estimate`, `compute/quote`, `cost/tokens`, `cost/gate`, `compute/actual`, `compute/acquire` (paid_compute), `compute/release` | operators | cairn-operator-compute | | | train-managed, continual-learning-managed, batch-inference; training template cost gates | OBSERVED; ADR-0097 |
| `[eval.finetune]`, `[eval.finetune-remote]` | pack capability (runtime eval recipe → `model/gate`) | training | `pack.toml` | `POST /runs/{id}/evaluate` (DOCUMENTED in pack.toml) | | OBSERVED manifest; route not verified here |
| Core eval machinery | pack capability | core | `src/cairn/core/eval/` (scoring, taxonomy, diagnosis, recipe, continuous), `storage/eval_ledger.py`, judges | `run.finished`/`run.failed` → the platform's `scoring` run hook (`dispatcher.py:574` `_terminal_hooks`, #2421) | | OBSERVED |

### Red team (code `cairn-operator-redteam`) — operators **REGISTERED** (#2289); the domain packs `redteam`, `redteam-kb` were retired in #2413
| Capability | Kind | Pack | Entry point | Implementation | Composes | Tag |
|---|---|---|---|---|---|---|
| `redteam/{scan,generate,execute,mutate,classify,aggregate,report}` | operators (7) | cairn-operator-redteam | `cairn/operators/official/redteam/pyproject.toml:26-33` | `garak` probes bought, never the dep (memory/ADR-0077) | | OBSERVED; workspace member and installed since #2289; **CONFLICT**: ADR-0077 Amendment 2 says "six operators specified, three delivered" while seven entry points exist |

### Incident & security (packs `incident`, `security-triage`, `security-kb`; code `cairn-operator-clickstack`, `cairn-operator-blast-radius`, `cairn-operator-git`, `cairn-operator-logs-analytics`, `cairn-operator-postmortem`, `cairn_platform.integrations.observability`, the platform since #2497)
| Capability | Kind | Pack | Entry point | Implementation | Composes | Tag |
|---|---|---|---|---|---|---|
| `incident/rca` | pipeline (3 legs) | incident | `pipelines/rca.yaml` | template incident-rca → gate accept-root-cause → template post-incident-postmortem | | OBSERVED; tests 13 |
| `incident-rca`, `blast-radius`, `change-correlation`, `orchestrated-incident`, `post-incident-postmortem` | templates | incident | `flows/*.md` | LangGraph; capability tools (`read_logs`, `read_traces`, `compute_blast_radius`, `correlate_change_with_incident`…) via `BackendProtocol` | skills `incident-orchestrated`, `post-incident-postmortem`; `[[demo.incident-rca]]`; 3 policies | OBSERVED |
| `security_triage/triage` | pipeline (1 leg) | security-triage | `pipelines/triage.yaml` | template triage-alert (`prompt`) grounded in `security-kb` role | | OBSERVED; tests 3 |
| `clickstack/{query_logs,query_traces,query_metrics,get_topology}`, `blast-radius/compute`, `git/correlate`, `logs/summarize`, `traces/analyze`, `postmortem/write_file` | operators | official packs | | `mcp_tool` executor also declared for clickstack | incident templates | OBSERVED |

### Platform substrate (builtins) and cross-domain operators
| Capability | Kind | Pack | Entry point | Implementation | Composes | Tag |
|---|---|---|---|---|---|---|
| `dataflow/run`, `ml/run` (doors), `artifact/promote` | operators (`remote_job`) | cairn-operator-dataflow | `pyproject.toml:32-35` | the kind-matched doors that submit a `DataflowSpec` | promote template | OBSERVED |
| engines `builtin`, `metaflow`, `skypilot` | engines | cairn-operator-dataflow | `pyproject.toml:39-42` (`cairn.dataflow_engines`) | `engines*.py`; SSH variants; `cairn-dataflow-run` job entry | selected by profile `dataflow_engine`/`ml_engine` (`dataflow/engine_registry.py:83`) | OBSERVED |
| `memory/search`, `memory/write`; `storage/put`, `storage/get` (local) and `s3/put`, `s3/get`; `rag/search`, `rag/fetch`, `rag/rerank`; `mcp/call`; `redact/text`; `audit/store`; `secrets/env`, `secrets/infisical` (both builtin); `monitor/threshold`, `monitor/propose`, `monitor/dispatch`; `notify/slack`; `lineage/record`; `report/model_card`; `llm/generate`; `remote/exec` | operators | builtins + official | each pack's `pyproject.toml` | bound through `[profile.bindings]` (`src/cairn/profiles/local-dev.toml`) to capabilities `memory.*`, `storage.*`, `rag.*`, `mcp.call`, `redact.text`, `audit.sink`, `secrets.store`, `notify.sink` | `monitor/dispatch` starts pipelines (`operator_monitor_threshold/operators/dispatch.py:50`) | OBSERVED |
| `train/lora` (capability `train.finetune`, contract `train.finetune/1.0.0`), stage `train.lora`, `train/eval-loss` (capability `train.eval_loss`, contract `train.eval_loss/1.0.0`) | operators (`in_process`) + ml stage | cairn-operator-train-local | `pyproject.toml` (`cairn.operators`, `cairn.dataflow_stages`) | `operator_train_local/finetune.py`: a CPU LoRA fine-tune (torch, transformers, peft behind the `[train]` extra; deterministic mocks without it) | hello-cairn `tiny-finetune` (ADR-0127 D1); a profile pins `base_model` under `[profile.config."train/lora"]` | OBSERVED; tests in the pack (`-m train` needs the extra) |
| Ambient knowledge / RAG roles | pack capability | core + knowledge packs | `services/rag/`, `compiler.py:1890 compose_ambient_knowledge` | | `security-kb` → `security_triage`; `dataset_readiness/rag/governance` | OBSERVED |

### CLI (`src/cairn/cli/main.py:61-130`, OBSERVED)
| Command | Kind | Starts / does |
|---|---|---|
| `cairn run <pack>/<template> \| <file.py>` | CLI command | compiles + runs a template in-process (`commands/run.py:332,348`) — **bypasses Dispatch** (C1) |
| `cairn preflight`, `cairn gate`, `cairn replay`, `cairn lineage`, `cairn eval`, `cairn test`, `cairn validate`, `cairn visualize`, `cairn export`, `cairn ls`, `cairn doctor`, `cairn lock` | CLI commands | preflight doors; approve gates; replay; lineage; eval recipes; pack tests; validation; graph; export a pipeline file; list; doctor; lockfile |
| `cairn pipeline {ls, show, explain, run}` | CLI group | `run` → `launch_pipeline` (`commands/pipeline.py:260`) — the governed door |
| `cairn flow {ls, split, export}` | CLI group | list/split/export flows to engine-native files |
| `cairn remote …` (19) | CLI group | drive a remote control plane via `cairn-client` (launch, watch, cancel-pipeline, presets, console TUI) |
| Ask (`ask {plan, run}`) — **platform only** | CLI group | NL → plan → run copilot (`cairn-ask`, part of cairn-platform; ADR-0127 D7, #2573). The platform's `cairn.cli` provider mounts it (`cairn_platform/cli/ask.py`, ADR-0134 D6); with no provider's `ask`, the public CLI mounts a hidden stub that says Ask is part of cairn-platform (`cli/main.py`, #2579). `/ask` is the platform's router (ADR-0123 D1) |
| `cairn init {pack, template, skill, policy, operator}`, `cairn pack {schema, check, add, build, sync}` | CLI groups | scaffolding + pack lifecycle |
| `cairn dev`, `cairn deploy`, `cairn worker`, `cairn demo`, `cairn compute {status, sweep}`, `cairn config workflow`, `cairn connect …` (6), `cairn pilot …` (13), `cairn series {start, ls, compare}`, `cairn knowledge …` (5), `cairn suggest …` (5) | CLI groups | dev stack; deploy; Temporal worker; demos; GPU sweep; workflow config; connections; pilots; series; knowledge corpora; suggestions |

## Relationships (OBSERVED unless tagged)

```
dataset_readiness/import ─ flow clean ─ stages (cairn-operator-dataset-checks, data-expectations) ─▶ dataset:<name>@N
                                                                                                  │ dataset: refs resolved at leg admission
training/e2e-finetune       ─ flow readiness ─▶ flow finetune (ml) ─ stages (cairn-operator-training) ─▶ gate promote-model
training/verifiable-finetune ─ facts-synth ─ facts-prep ─ closedbook-eval ─ novelty ─ finetune ─ serve-adapter(HITL) ─ closedbook-eval ─ promote
training/continual-learning-managed ─ compute/acquire ─ [cycle …finetune… serve/start … eval/delta … promote] ─ serve/stop ─ compute/release
compress/distill ─ task compress/distill (cairn-operator-training) ─ gate            compress/quantize ─ template quantize ─ gate
compress/distill-compare · training/closedbook-compare ─ closedbook-eval ×2 ─ eval/delta ─ gates       (evaluation as legs)
serving/serve ─ template serve (serve/vllm, infer/bench) ─ gate accept-endpoint ─▶ endpoint ─ consumed by closedbook-eval post legs
serving/batch-inference ─ cost/tokens ─ gate cost ─ flow batch-infer (infer.batch) ─ cost/tokens
incident/rca ─ template incident-rca ─ gate ─ template post-incident-postmortem
```
- Cross-pack **template** composition (ADR-0084 D5 says none): `training/finetune.md`,
  `finetune-remote.md`, `composed-hitl-smoke.md` subgraph `dataset-readiness/check`
  (compiler warns, `engine/compiler.py:1115`). **CONFLICT.**
- Cross-pack **stage-list copying** (D5 as intended): `closedbook-eval` exists in `training`
  and `compress`; `readiness`/`data-prep` in `training` mirror `dataset_readiness/clean`.
- Cross-pack **operator reuse** (allowed): `compute/*`, `serve/*`, `eval/*`, `dataset/replay`
  appear as task legs in training pipelines; `compress/distill` is implemented in
  cairn-operator-training (naming vs ownership).
- Capability binding: `[profile.bindings]` maps `memory.*`, `storage.*`, `rag.*`, `mcp.call`
  (local-dev), `redact.text`, `audit.sink` (secure) and `secrets.store` (prod-infisical →
  `secrets/infisical`; unbound → `NullSecretStore`) to builtin operators; `train.run` is
  provided by six operators and chosen per binding/`when`.
- The dataset → training → evaluation → compression → serving chain that the README implies
  exists as **separate pipelines joined by artifacts** (`dataset:` refs, adapter URIs,
  endpoints), not as one pipeline. INFERRED from the records above; `e2e-fullstack` and
  `continual-learning-managed` are the longest single chains.

## Invocation
| Capability | CLI | API | Pipeline | Programmatic | Discovery |
|---|---|---|---|---|---|
| any pipeline `<pack>/<name>` | `cairn pipeline run <ref> --set k=v` (`commands/pipeline.py:292`) · `cairn remote launch` | `POST /start {"ref","inputs","kind":"pipeline"}` (`routers/start.py:114`) · `POST /pipelines/launch` (`routers/pipelines.py:455`) · preview/explain `POST /pipelines/preview`, `GET /pipelines/{pack}/{name}/explain` | as a whole; legs are not addressable | `cairn_client.launch_pipeline` (`packages/cairn-client/src/cairn_client/__init__.py:288`); `cairn.core.dispatch.pipeline_launch.launch_pipeline`; `cairn_sdk.run_pipeline` (SDK-compiled packs) | `cairn pipeline ls`; `GET /pipelines` (installed list, `routers/pipelines.py:522,328`) |
| any agentic template | `cairn run <pack>/<template> --interactive|--mock|--dry-run` (the dev loop: in-process, no record — ADR-0106 D6) | `POST /start` with a bare template ref is REFUSED (`FlowNotRunnableError`, `dispatch/start.py:69`, ADR-0106 D1) — start the declared one-leg pipeline that wraps it · `POST /packs/{pack}/{template}/run` (`routers/templates.py:87`) returns the same refusal (`:117`) | `template:` leg | `cairn_client.run_template` (`:254`), `preflight` (`:257`) | `cairn ls`; `GET /packs` |
| a data/ml flow | `cairn flow ls/export`; a flow is a component (ADR-0106 D1) — run it through a declared one-leg pipeline (`$inherit`, D2) | `POST /start` with a bare flow ref is refused (`start.py:69`) | `flow:` leg | `execute_leg` (internal) | `GET /flows/{pack}/{name}/stages` (`routers/pipelines.py:301`) |
| a single operator | — (task legs only; `cairn test` runs pack tests) | — | `task: <op>` leg (`pipeline_bridge.py:1297`) | `invoke_operator` (`operator/runtime.py:888`) — internal | `cairn ls`; `GET /operators/reports`; manifests |
| gates / approvals | `cairn gate` | `POST /runs/{id}/resume` (`run_control.py:50`), `POST /pipelines/{id}/steps/{step}/{advance,approve}` (`pipelines.py:477,486`), `POST /runs/{id}/proposals/{seq}/{approve,reject}`, `GET /triggers/approve/{workflow_id}` (Slack link) | `gate:` leg | `cairn_client.resume_run`, `approve_step` | console Decisions inbox |
| triggers | — | `POST /triggers/{alert,pattern,manual}` (`ingress.py:32-62`); declared `triggers:` front matter → schedule/webhook/watch stores | pipeline `on:` | | `GET /triggers/{skills,scheduled}` |
| control | `cairn remote cancel-pipeline` | `POST /runs/{id}/cancel`, `/pipelines/{id}/cancel`, `/runs/{id}/control` | | `cairn_client.cancel_run/cancel_pipeline` | SSE `GET /runs/{id}/stream` |
| MCP | Ask, platform only (uses the ops MCP) | `/mcp` mount when enabled (`triggers/mcp.py:144 build_ops_mcp`) — read-only route map (runs, insights, packs, policies, catalog…; no start routes exposed, `mcp.py:43-100`) | | | |
| pack discovery | `cairn ls`, `cairn doctor`, `cairn pack check` | `GET /packs`, `GET /packs/{pack}/{template}/contract` | | `cairn.core.pack.ensure_loaded()` | entry-point group `cairn.packs`; `CAIRN_PACK_PATH` |

## Classification summary
- **OBSERVED**: every pipeline/flow/template/operator/CLI row above (parsed from files at HEAD).
- **DOCUMENTED (not re-verified here)**: `POST /runs/{id}/evaluate` for `[eval.*]` recipes (pack.toml comment); garak-as-probes for redteam (ADR-0077); README's chain narrative.
- **INFERRED**: the `*-smoke` templates are shipped test fixtures; the dataset→train→eval→compress→serve chain is artifact-joined rather than one pipeline; `clean.md` vs `clean.yaml` disambiguation by leg kind.
- **CONFLICT**: README "Eight packs / 14 runnable pipelines" vs 9 registered packs / 28 installed pipelines (31 on disk); ADR-0084 D5 vs the three templates that still subgraph `dataset-readiness/check`; ADR-0077 Amendment 2 "three operators delivered" vs seven entry points on disk.

## Missing / unclear
| Kind | Finding |
|---|---|
| In docs, not in code | README lists `training/verifiable-finetune` as "8 legs"; the file and `cairn pipeline ls` show 10 (`readiness` "2 legs" and `e2e-fullstack` "7 legs" do match). README's `compress/quantize` "quantize → re-eval → gate against baseline → HITL promote" describes the template body; the pipeline is 2 legs (template + gate). |
| In code, not in docs | 20 of the 28 installed pipelines are absent from README's list of 8 (11 `training/*` beyond `verifiable-finetune`/`e2e-fullstack`, `compress/distill`, `compress/distill-compare`, `compute/price-a-run`, `eval/score-a-model`, `serving/batch-inference`, `serving/export-model`, `dataset_readiness/{pull-and-admit,api-and-admit,watch-source}`). `docs/status.yaml` has no redteam probe. |
| Duplicate / overlapping | `closedbook-eval` flow ×2 (training, compress); `readiness`/`data-prep`/`facts-prep` are near-copies of `clean`; two training implementations (`finetune.yaml` flow vs `finetune.md` template); `storage/put|get` provided by both `artifact_local` and `s3` (intended: profile picks); `serve/vllm` vs `serve/start` both provide `model.serve`; `train/stub` vs `train/local` both `train.run`, `read_only`. |
| Unclear ownership | `compress/distill` implemented in cairn-operator-training; `monitor/dispatch` (a builtin) drives the dispatcher and launch door; `compute/*` used by training pipelines but owned by the compute pack; core modules `recommender.py`/`models_catalog.py` read training-pack data. |
| Naming inconsistencies | `dataset-readiness` (pack name) vs `dataset_readiness` (ref prefix in `cairn pipeline ls`); `security-triage` vs `security_triage`; `obs-operator-*` names in `cairn.lock` vs `operator-*` distribution names; `template` (leg kind) vs `flows/*.md` (file location) vs "workflow" (registry) for the same thing; `clean.md`/`clean.yaml`; `sample.rows` (dataset_checks) vs `sample.fidelity` (training) in one global stage namespace. |
| Registration gaps | none since #2413 retired the `redteam`, `redteam_kb` domain packs and the `operator-rag-parse` wheel (`cairn-operator-redteam` joined the workspace in #2289). |
| Untested runnable units | **`serving/export-model` only** (0 references anywhere under `tests/` or a pack's `tests/`). *Correction 2026-09-10:* the first pass counted with a narrow quoted-name grep and wrongly listed `training/closedbook-compare`, `dataset_readiness/{pull-and-admit,api-and-admit}` and the `redteam/*` pipelines as untested; re-counted by whole-file reference, they have 1, 1, 1 and `tests/core/test_redteam_campaign_pipeline.py` respectively (the `redteam/*` pipelines and that test were retired in #2413). |

## Suggested targets
- `/understand`: `src/cairn/core/dispatch/start.py` (why every start becomes a pipeline); `src/cairn/core/dataflow/defs.py` (how a flow ref resolves and where the stage namespace is global); `cairn/operators/official/training/src/operator_training/flow_stages.py`; `src/cairn/core/pack/loader.py` (why redteam is invisible); `cairn/packs/training/flows/finetune.md` vs `finetune.yaml` (the two training paths).
- `/trace`: `cairn pipeline run training/train-only` end to end; `POST /start {"ref":"dataset_readiness/import"}`; `serving/batch-inference` through the cost gate into `infer.batch`; a declared `watch` trigger firing `dataset_readiness/watch-source`; `cairn run training/finetune` (the bypass path) side by side with `POST /start` for the same ref; `redteam/quick` after registering the pack via `CAIRN_PACK_PATH`.
