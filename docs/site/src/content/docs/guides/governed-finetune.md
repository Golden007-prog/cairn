---
title: A governed fine-tune
description: Gate the training data, fine-tune with LoRA on CPU, gate on held-out loss, tag the adapter, ask a person, write the card — tiny-finetune end to end.
---

`tiny-finetune` is the ml pipeline of `hello-cairn`. It trains a real LoRA
adapter for a small model on CPU, then applies the governance a model needs
before anyone uses it.

```
profile → data gate → train (ml flow) → eval → loss → loss gate
  → promote ⏸ → review ⏸ → card
```

Features used: [training](/features/training/),
[gates](/features/gates/), [dataflow and artifacts](/features/dataflow-and-artifacts/#promote),
[monitor](/features/monitor-and-hooks/#monitor),
[governance](/features/governance/#budget).

## 1. Install the training stack

```bash
uv sync --extra dev --extra hello --extra train   # train = cairn-operator-train-local[train]
export CAIRN_PROFILE=examples/hello-cairn/profiles/hello.toml
cairn dev --engine-only --port 8471 &
cairn remote login -u dev -p dev -e http://localhost:8471
```

The `[train]` extra is torch, transformers, peft and datasets; CPU is enough.
There is no torch wheel for Intel macOS for the locked torch.

## 2. Launch

```bash
cairn pipeline explain hello-cairn/tiny-finetune -e sandbox
cairn remote launch hello-cairn/tiny-finetune --environment sandbox --watch
```

`base_model` defaults to `sshleifer/tiny-gpt2`, which trains in seconds. Pass
`-i base_model=Qwen/Qwen2.5-0.5B` for a real small model (minutes).

## 3. What each leg does

**The data gate.** `profile` (`data/profile`) reads
`examples/hello-cairn/data/train.jsonl` and checks the `text` role; `data-ok`
passes only on a `pass` verdict with at least 8 rows.

```yaml
- gate: data-ok
  depends_on: [profile]
  predicate: steps.profile.verdict == 'pass' && steps.profile.record_count >= `8`
```

**Train.** `train` runs the `lora` ml flow on the builtin ml engine: one
`train.lora` stage. The stage reports a model, so the leg registers the adapter
and exposes it as `candidate_artifact_id`; `produces: {adapter_path: Model}`
makes `$steps.train.adapter_path` the adapter directory.

**Measure.** `eval` (`train/eval-loss`, read-only) scores the adapter on the
held-out rows.

**The loss gate.** `loss` (`monitor/threshold`) compares `eval_loss` with
`target_loss` (default 12.0, `comparator: gt`), and `loss-ok` stops the
pipeline when it crossed:

```yaml
- gate: loss-ok
  depends_on: [loss]
  predicate: steps.loss.crossed == `false`
```

Try `-i target_loss=0.1` to watch the gate stop the pipeline before anything
is tagged.

**Promote.** `promote` (`artifact/promote`) points `hello-adapter@candidate`
at the adapter. The operator declares `requires_approval`, so the tag moves
only after a person approves:

```bash
cairn remote advance <pipeline_id> promote
```

**Review.** `review`, a person gate, shows the training loss, the held-out
loss, the threshold, the tag and its URI:

```bash
cairn remote advance <pipeline_id> review
```

**The card.** `card` (`storage/put`) writes the promoted URI to the
`hello-models` store as `tiny-finetune-card`.

## 4. What the record shows

```bash
cairn remote findings <pipeline_id>
```

The outputs are the adapter path, the held-out loss, the promoted artifact id
and the card's URI. The pipeline's budget (`usd: 1`) is reserved and settled
on the record; a CPU fine-tune bills nothing, so every leg records a cost of
0.

## Limits

- CPU fine-tunes of small models only. GPU training, model registries and
  model evaluation suites are part of cairn-platform.
- The two person decisions (`promote` and `review`) are separate on purpose:
  the tag is one decision, the card another.
