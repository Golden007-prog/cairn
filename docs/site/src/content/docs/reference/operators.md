---
title: Builtin operators
description: The operators that ship with the engine — every builtin operator pack, its operations, capability, risk class, and where the example uses it.
---

The engine ships 14 builtin operator packs, each its own package
(`cairn-operator-<name>`), and each operation implements a capability contract
from a `cairn-contracts-<domain>` package. A profile binds a capability to an
operation (`"storage.put" = "storage/put"`); a pipeline names the operation as
a `task:` leg, or a template names it (or its capability) in an `operation`
node.

**Risk** classes drive gates and restart behaviour: `read_only`,
`external_write`, `production_mutation`, `paid_compute`. **Approval**: the
operation waits for a person before it runs, wherever it is used.

| Package | Operation | Capability | Risk | Approval | In `hello-cairn` |
|---|---|---|---|---|---|
| `artifact-local` | `storage/put` | `storage.put` | external_write | | stage, publish, card |
| | `storage/get` | `storage.get` | read_only | | bound, not called |
| `audit-store` | `audit/store` | `audit.sink` | read_only | | audit legs |
| `data-profile` | `data/profile` | `data.profile` | read_only | | profile legs |
| `dataflow` | `dataflow/run` | `dataflow.run` | external_write | | (data flow legs run on its `builtin` engine) |
| | `ml/run` | `ml.run` | external_write | | (the `lora` ml flow) |
| | `artifact/promote` | `artifact.promote` | external_write | yes | promote legs |
| `mcp` | `mcp/call` | `mcp.call` | external_write | | the assistant's `word_count` call |
| `memory-local` | `memory/search` | `memory.search` | read_only | | bound |
| | `memory/write` | `memory.write` | production_mutation | | the assistant's memory write |
| `monitor-threshold` | `monitor/threshold` | `monitor.threshold` | read_only | | `length`, `loss` |
| | `monitor/propose` | `monitor.propose` | read_only | yes | nightly-ops `propose` |
| | `monitor/dispatch` | `monitor.dispatch` | paid_compute | | not used |
| `rag-local` | `rag/search` | `rag.search` | read_only | | the assistant's retrieval |
| | `rag/fetch` | `rag.fetch` | read_only | | bound |
| `rag-rerank` | `rag/rerank` | `rag.rerank` | read_only | | the assistant's re-rank (identity) |
| `redact-regex` | `redact/text` | `redact.text` | read_only | | profile-and-publish `redact` |
| `remote` | `remote/exec` | `remote.exec` | external_write | | the nightly job's SSH report |
| `secrets-env` | `secrets/env` | `secrets.store` | read_only | | the nightly job's secret check |
| `secrets-infisical` | `secrets/infisical` | `secrets.store` | read_only | | under `hello-infisical.toml` |
| `train-local` | `train/lora` | `train.finetune` | external_write | | the `train.lora` stage |
| | `train/eval-loss` | `train.eval_loss` | read_only | | tiny-finetune `eval` |

The dataflow pack also provides the flow stages `noop` and `noop.slow`, and
the dataflow engine `builtin`. The `metaflow` and `skypilot` engines are their own optional
wheels, `cairn-operator-dataflow-metaflow` and `cairn-operator-dataflow-skypilot`, installed with
`cairndev[metaflow]` and `cairndev[skypilot]`.

## Extras

| Package | Extra | Adds |
|---|---|---|
| `remote` | `ssh` | asyncssh |
| `secrets-infisical` | `infisical` | the Infisical SDK |
| `train-local` | `train` | torch, transformers, peft, datasets |
| `rag-rerank` | `cross_encoder` · `cohere` | a cross-encoder reranker; Cohere Rerank (needs `COHERE_API_KEY`) |
| `dataflow-metaflow` | `metaflow` | the Metaflow library (or install `cairndev[metaflow]`) |
| `dataflow-skypilot` | `skypilot` | SkyPilot with its cloud providers (or install `cairndev[skypilot]`) |

The engine's root extras `ssh`, `infisical` and `train` pull the first three.

## Writing your own

```bash
cairn init operator my_ops --op my/thing
```

scaffolds an operator pack with the entry-point wiring, schemas and a test
skeleton. See [Operators](/concepts/operators/).

Other operator packs (dataset checks, evaluation, training on GPUs, serving,
model registries, incident tooling) are part of cairn-platform.
