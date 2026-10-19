---
title: Training
description: A real LoRA fine-tune on CPU as an ml flow leg (train/lora, train.finetune), a held-out loss (train/eval-loss), and the [train] extra.
---

The builtin train-local pack (`cairn-operator-train-local`) fine-tunes a
small causal language model with LoRA on the engine's own box, CPU is enough,
and scores the adapter on held-out rows.

| Operator | Capability | Used as |
|---|---|---|
| `train/lora` | `train.finetune` (contract `train.finetune/1.0.0`) | the ml stage `train.lora` in a `kind: ml` flow, or a task |
| `train/eval-loss` | `train.eval_loss` | a read-only task leg |

The `train.lora` stage reports `kind: model`, so the flow leg registers the
adapter as a model artifact and exposes it as `candidate_artifact_id`, which
`artifact/promote` can tag.

## Setup

The training stack is a separate, large install: the pack's `[train]` extra
(torch, transformers, peft, datasets). The engine's root `train` extra pulls
it:

```bash
uv sync --extra dev --extra hello --extra train
```

Install it wherever the ml engine runs; with `ml_engine = "builtin"` that is
the engine's box.

```toml
# examples/hello-cairn/profiles/hello.toml
[profile.backends]
ml_engine       = "builtin"      # tiny-finetune's train.lora runs on this box
```

## The ml flow

```yaml
# examples/hello-cairn/flows/lora.yaml
name: lora
kind: ml
description: >
  LoRA fine-tune a small causal LM on a JSONL of {"text": ...} rows (CPU).
stages:
  - stage: train.lora
    config:
      dataset: $config.dataset
      base_model: $config.base_model
      lora_rank: $config.lora_rank
      steps: $config.steps
      max_samples: $config.max_samples
      mock: $config.mock
```

Every `$config.<key>` the leg does not bind is dropped, so the stage's own
defaults apply (`Qwen/Qwen2.5-0.5B`, 8 steps, 64 rows). `mock: true` runs a
torch-free stand-in with deterministic numbers.

## Configuration

`train/lora` input: `dataset` (a JSONL path or `file://` URI of
`{"text": ...}` rows), `base_model` (a Hugging Face id or a local directory),
`lora_rank` (default 8), `steps` (default 8), `max_samples` (default 64),
`learning_rate` (default `2e-4`), `max_seq_len` (default 256), `output_dir`.
Output: `adapter_path`, `train_loss`, `eval_loss`, `tokens_seen`,
`steps_run`, `base_model`, `elapsed_s`.

`train/eval-loss` input: `adapter_path`, `base_model`, `dataset`,
`max_samples`, `max_seq_len`. Output: `eval_loss`, `tokens_seen`.

## Example: `tiny-finetune`

```yaml
# examples/hello-cairn/pipelines/tiny-finetune.yaml (excerpt)
- id: train
  label: Fine-tune with LoRA
  flow: lora
  depends_on: [data-ok]
  timeout: 30m
  produces: {adapter_path: Model}
  with:
    dataset: $input.dataset
    base_model: $input.base_model
    steps: $input.steps
    max_samples: $input.max_samples
    lora_rank: $input.lora_rank

- id: eval
  label: Measure the held-out loss
  task: train/eval-loss
  depends_on: [train]
  with:
    adapter_path: $steps.train.adapter_path
    base_model: $input.base_model
    dataset: $input.dataset
    max_samples: $input.max_samples
```

A flow leg's stats are namespaced by stage; `produces:` declares the leg's
product so `$steps.train.adapter_path` is the adapter directory. The pipeline
then gates on the loss, tags the adapter, asks a person and writes a card: see
the [governed fine-tune guide](/guides/governed-finetune/).

```bash
cairn remote launch hello-cairn/tiny-finetune --environment sandbox --watch
cairn remote launch hello-cairn/tiny-finetune -i base_model=Qwen/Qwen2.5-0.5B --watch
```

`base_model` defaults to the stand-in `sshleifer/tiny-gpt2` (seconds);
`Qwen/Qwen2.5-0.5B` trains in minutes.

## Limits

- CPU training of small models only. GPU training, remote training providers
  and model registries are part of cairn-platform.
- There is no torch wheel for Intel macOS for the locked torch: the training
  stack does not install there.
- `eval_loss` from `train/lora` is null when there are too few rows to hold
  some out; `train/eval-loss` scores the adapter separately.
