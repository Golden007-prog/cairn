---
title: Pipelines
description: The one runnable unit — a declared pipeline whose legs are tasks, flows (data or ml) and agentic templates, each its own governed run.
---

A **pipeline** is the only thing cairn runs. It is a YAML file under a pack's
`pipelines/` directory that lists **steps**. Each step is one of:

| Step | Declared as | What runs |
|---|---|---|
| task leg | `task: <pack>/<op>` | one operator invocation, behind the approval gate |
| flow leg | `flow: <name>` | a `kind: data` or `kind: ml` stage list on a dataflow engine |
| template leg | `template: <name>` | an agentic markdown template, compiled to a LangGraph graph |
| gate | `gate: <id>` | a decision: a predicate over data, or a person (see [Gates](/features/gates/)) |

Every task, flow and template leg is **its own run**, with its own record,
cost, audit and approval state. The pipeline record ties the legs together.

A bare flow or template ref is refused at the start door: a flow runs only as
a leg of a declared pipeline. If a pipeline and a flow share a ref, the
pipeline wins.

## The pipeline kinds the example ships

`examples/hello-cairn` has one pipeline per kind of work. Together they cover
every leg type.

| Pipeline | Kind | Legs |
|---|---|---|
| `profile-and-publish` | data | `data/profile` → data gate → `redact/text` → flow `engine-check` → `storage/put` → person gate → `artifact/promote` → `storage/put` (emits `dataset.ready`) → `audit/store` |
| `ask-the-docs` | agentic | template `assistant` → `monitor/threshold` → person gate |
| `tiny-finetune` | ml | `data/profile` → data gate → flow `lora` (`kind: ml`) → `train/eval-loss` → `monitor/threshold` → data gate → `artifact/promote` → person gate → `storage/put` |
| `nightly-ops` | flow + operations | template `nightly-job` → flow `summarise` → `monitor/propose` → `audit/store` |

### Flow kinds

A flow file declares `kind: data` or `kind: ml` and a list of stages. The
public core ships one stage provider, the builtin dataflow pack, with the
conformance stages `noop` and `noop.slow`; the builtin train-local pack adds
the ml stage `train.lora`.

```yaml
# examples/hello-cairn/flows/summarise.yaml
name: summarise
kind: data
description: >
  Carry the nightly report through the builtin dataflow engine: two conformance
  stages, nothing transformed.
stages:
  - stage: noop
  - stage: noop.slow
    config:
      seconds: 1
```

The **agentic** kind is a markdown template with YAML front matter
(`flow.type: state_machine` in the example), whose `operation` nodes call
operators and whose `prompt` nodes call a model. See
[Models and agents](/features/models-and-agents/).

## Setup

Nothing beyond the engine for task, data-flow and template legs. A pack is
found either as an installed wheel (the `cairn.packs` entry point) or as a dev
directory:

```bash
uv sync --extra dev --extra hello --extra rag
export CAIRN_PROFILE=examples/hello-cairn/profiles/hello.toml
# or, without installing the example wheel:
export CAIRN_PACK_PATH=examples/hello-cairn
```

The profile picks the engines:

```toml
# examples/hello-cairn/profiles/hello.toml
[profile.backends]
dataflow_engine = "builtin"      # the builtin dataflow engine (noop stages)
ml_engine       = "builtin"      # tiny-finetune's train.lora runs on this box
```

## Configuration

A pipeline file carries `inputs`, `governance` (budget, series, approvers),
`triggers` and `steps`. Values wire between legs with `$input.<name>` and
`$steps.<id>.<field>`; a `$jmes:` expression reaches into nested output.

```yaml
# examples/hello-cairn/pipelines/profile-and-publish.yaml (excerpt)
steps:
  - id: profile
    label: Profile the dataset
    task: data/profile
    with:
      source: $input.path
      expectation: $input.expectation

  - gate: data-ok
    depends_on: [profile]
    predicate: steps.profile.verdict == 'pass' && steps.profile.record_count >= input.min_rows

  - id: engine-check
    label: Run the data flow
    flow: engine-check
    depends_on: [redact]
    with:
      source: $input.path
      register: hello-sample
```

Step fields used by the example: `id`, `label`, `task` / `flow` / `template`
/ `gate`, `depends_on`, `with`, `predicate`, `approvers`, `show`, `emits`,
`timeout`, `produces`.

## Run it

```bash
cairn pipeline explain hello-cairn/profile-and-publish -e sandbox   # what will happen
cairn validate examples/hello-cairn                                  # offline check

cairn dev --engine-only --port 8471 &
cairn remote login -u dev -p dev -e http://localhost:8471
cairn remote launch hello-cairn/profile-and-publish --environment sandbox --watch
```

`cairn pipeline run hello-cairn/<name>` launches through the same door from a
local CLI process, and `POST /start {"ref": "hello-cairn/<name>"}` does it
over HTTP. `explain` lists each leg with the operator or flow it resolves to,
the gate that answers it, and the budget, series and approvers.

## Limits

- The public core's only data stages are `noop` and `noop.slow`: they prove
  the flow-leg path and transform nothing. Real kernels come from other
  operator packs.
- `cairn run --interactive | --mock | --dry-run` is a dev loop that compiles
  and runs a flow in-process and writes no pipeline record.
- Under the Temporal runtime a `task:` leg fails; see
  [Backends](/features/backends/).
