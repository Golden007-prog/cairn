---
title: Gates
description: Stop a pipeline for an answer that is not code — a predicate over data, a person, or a gate an operator declares and a pipeline cannot remove.
---

A gate stops a pipeline until something answers it. cairn has three kinds,
and every one of them is decided through the same door and recorded on the
pipeline record.

| Gate | Answered by | Declared in |
|---|---|---|
| **data gate** | a predicate over an earlier leg's output | the pipeline: `gate:` + `predicate:` |
| **person gate** | a person approving or rejecting | the pipeline: `gate:` + `approvers:` + `show:` |
| **operator gate** | a person, before the operator runs | the operator's manifest: `requires_approval = true` |

A fourth source, **policy**, can add a gate to any operator call at plan time
or at the call itself; see [Governance](/features/governance/).

The rule that ties them together: **a pipeline can add a gate, never remove
one** an operator or a policy demands.

## Data gate

A data gate resolves the moment the leg it reads finishes. True passes with
verdict `data`; false is a fail-closed stop (`gate_blocked`), and nothing after
it runs.

```yaml
# examples/hello-cairn/pipelines/profile-and-publish.yaml
- gate: data-ok
  depends_on: [profile]
  predicate: steps.profile.verdict == 'pass' && steps.profile.record_count >= input.min_rows
```

`tiny-finetune` gates on a model's held-out loss the same way, through
`monitor/threshold`:

```yaml
# examples/hello-cairn/pipelines/tiny-finetune.yaml
- id: loss
  task: monitor/threshold
  depends_on: [eval]
  with:
    metric: held-out loss
    value: $steps.eval.eval_loss
    threshold: $input.target_loss
    comparator: gt

- gate: loss-ok
  depends_on: [loss]
  predicate: steps.loss.crossed == `false`
```

## Person gate

The pipeline pauses with the `show:` fields on the approval card until someone
approves or rejects it.

```yaml
# examples/hello-cairn/pipelines/profile-and-publish.yaml
- gate: review
  depends_on: [stage]
  approvers: data-owners
  show:
    - profile.verdict
    - profile.record_count
    - redact.span_count
    - redact.text
    - stage.uri
```

Decide it from the CLI or over HTTP:

```bash
cairn remote advance <pipeline_id> review
# POST /pipelines/{pipeline_id}/steps/review/approve
```

`cairn remote advance` approves a gate (`awaiting_approval`) or releases any
other step waiting on a person (`awaiting_advance`). Under auth the server
records the signed-in identity as the approver; with auth off, `--approver`
names who decided.

## Operator gate

An operator that declares `requires_approval` waits for a person before it
runs, wherever it is used. In the public core, `artifact/promote` and
`monitor/propose` declare it. So `profile-and-publish` stops a second time, at
`promote`, even though the pipeline puts no gate there:

```bash
cairn remote advance <pipeline_id> review        # the person gate
cairn remote advance <pipeline_id> promote       # the operator's own gate
```

The operator gate is decided on the leg's one run: approving it does not
create a second run.

## Setup

Nothing to install: gates are part of the engine. To decide gates as a named
person, start the engine with auth on (see [Deployment](/deployment/)) and
sign in with `cairn remote login`.

## Limits

- A pipeline's `approvers:` is recorded on the gate and shown on the card, but
  it is **not enforced** today: any signed-in user can decide a gate.
- In the feature proof, gates decided through the CLI with auth off record no
  approver identity.
- A gate parked `awaiting_approval` survives a restart and is decided
  afterwards; see [Recovery](/features/recovery/).
