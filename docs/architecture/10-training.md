# Training

Training is a **domain**, not an engine subsystem: it exists as the `training` domain pack
(records), the `cairn-operator-training` operator pack (code), and a handful of core modules
that hold training-specific state (`storage/training_runs.py`, `mid_training.py`,
`training_history.py`, `recommender.py`, `models_catalog.py`, `model_license_gate.py`,
`fidelity.py`, `holdout.py`, `operator/pins.py`). Full trace: [../golden-paths/training.md](../golden-paths/training.md).

## Two ways to train (OBSERVED)
| Path | Record | Executes on | Governance |
|---|---|---|---|
| **ml flow** (ADR-0082, the current path) | `cairn/packs/training/flows/finetune.yaml` `kind: ml`, stages `sample.fidelity → train.qlora → eval.checkpoints → package.adapter`; used as a `flow:` leg by 13 pipelines (`pipelines/verifiable-finetune.yaml`, `train-only.yaml`, `train-managed.yaml`, `e2e-*.yaml`, `dpo-*.yaml`, `continual-learning*.yaml`, `facts-window.yaml`, `proxy-rank.yaml`, `closedbook-*.yaml`) | `execute_leg` → the environment's `ml_engine` (builtin / metaflow / skypilot / ssh) → `operator_training.flow_stages` kernels via `cairn.flow_stages` | whole-flow risk gate, budget reservation, policy-injected gates, HITL gates in the pipeline (`novelty`, `serve-adapter`, `promote-model`) |
| **agentic template** (the original, ADR-0039) | `cairn/packs/training/flows/finetune.md` (`state_machine` BT: `subgraph_node dataset-readiness/check` → cost estimate/gate/audit → `run-train` → cost actual → benchmark → model gate → HITL `model/register` → synthesize) with `operations/*.yaml` bindings to `train/*` provider operators | LangGraph via `run_template`; `train/{stub,local,modal,kaggle,runpod,vast}` operators (`remote_job` executor for paid providers) | per-operation `enforce_gate`, cost gate operators, `requires_approval` on `model/register` |

Both exist and are shipped. INFERRED: the ml-flow path is the strategic one (ADR-0084 D1/D2,
ADR-0102); the template path remains the demo/`[[demo.finetune]]` surface. The pack's
`pack.toml` says "runtime:langgraph" tag and "Zero domain Python: a markdown state_machine
template" — describing the template path only.

## Owns
- Domain pack `cairn/packs/training/` — 13 pipelines, 17 flows, 31 operation bindings, `schemas/`, `models/` (curated base-model catalog, data), `presets/`, `recommender/` (data read by `src/cairn/core/recommender.py`), `DESIGN.md`, `[[demo.finetune]]` tiers, `[eval.finetune]` runtime eval recipe.
- Operator pack `cairn/operators/official/training/` — operators `train/stub`, `train/local` (real LoRA on CPU/MPS), `train/modal`, `train/kaggle`, `train/runpod`, `train/vast`, `train/stage-dataset`, `train/teardown`, `train/proxy-trial`, `compress/distill`; flow stages `sample.fidelity`, `train.qlora`, `eval.checkpoints`, `package.adapter`, `train.dpo`, `dataset.dpo_pairs`; helpers `checkpoint_sync`, `dataset_fetch`, `env_fingerprint` (`trainer_env` reproducibility fingerprint), `reporting` (ADR-0103 `TRAIN_REPORTS`); heavy deps behind `[local]`/`[gpu]`/`[track-mlflow]` extras; `DESIGN.md` present.
- Core: `storage/training_runs.py` (history table), `operator/pins.py` (dataset hash + image digest + spec at submit), `mid_training.py` (checkpoint relay / mid-run metrics), `training_history.py`, `recommender.py`, `models_catalog.py`, `model_license_gate.py`, `fidelity.py`, `holdout.py`, `proxy_ranking.py`, `judge_*`.

## Does not own
Dataset admission (dataset-readiness pack), serving the trained adapter (serving pack),
evaluation harnesses (eval, eval_lm, eval_judged packs), model registry (`cairn-operator-model-registry`),
GPU provisioning as a governed leg (`compute` pack, ADR-0097).

## Public interface
The pipeline inputs (`TrainSpec` fields surfaced at the pipeline input layer, ADR-0102 D2),
`train.run` capability, `TrainSpec`/`TrainResult` schemas (`operator_training.schemas.train`),
`cairn pipeline run training/<pipeline>`, `cairn run training/finetune`.

## Dependencies
Operator pack → `cairn.core.operator` (22 edges), `cairn.core.<top>` (8: fidelity, connections…), `cairn.core.storage` (1). Domain pack → `cairn.core.pack`. Core → **`cairn.packs.training`** (2 lazy imports, a boundary violation: `models_catalog.py:100`, `recommender.py:102`).

## Side effects
Paid GPU rentals (Modal/RunPod/Vast/SkyPilot), HF Hub downloads and uploads (`artifact_target=hf`), MLflow/W&B tracking, S3 checkpoints, SSH to owned boxes, MPS/CPU training locally.

## State
`training_runs` table, `Run.train_pins`, checkpoints in the artifact store, MLflow (external), Optuna study DB (`optuna_storage_url`).

## Entry points
`POST /start {ref: "training/verifiable-finetune"}`, `cairn pipeline run training/...`,
console Pipelines → launch dialog, `cairn run training/finetune` (template path, CLI local).

## Tests
`tests/core/test_training_pack.py`, `test_verifiable_finetune_pipeline.py`,
`test_continual_learning_pipeline.py`, `test_train_pins.py`, `test_training_history.py`,
`test_mid_training.py`, `test_finetune_dataset_preflight.py`, `test_flatten_finetune.py`,
`test_wire_finetune_remote_compile.py`, `test_assemble_finetune_report.py`;
pack-local `cairn/operators/official/training/tests/` (17 files: flow stages, checkpoint
sync/scoping, dataset fetch, env fingerprint, trainer pins, validate mode, proxy trial, distill).

## Invariants / findings
- Reproducibility pins at submit (ADR-0082 P0) — CONFIRMED on the operator path (`pins.py`); the 08-13 audit notes `image_digest` is structurally `""` on flow legs (DOCUMENTED; not re-verified).
- Real GPU work runs on a box that installs `cairn-operator-training[local|gpu]`; the control plane image must not carry torch — DOCUMENTED (`pyproject.toml` `[kernel]` comment, `Dockerfile.engine` `PRODUCTION_TIER`).
- The pack's `pack.toml` description describes only the template path — minor DOCUMENTATION drift.
