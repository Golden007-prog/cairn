---
title: Dataflow and artifacts
description: Flow legs on the builtin dataflow engine, the artifact registry and local store, governed promotion, and dataset versions.
---

A **flow leg** runs a stage list on a dataflow engine as one governed run. The
engine reports what the leg produced as artifact **descriptors**, and the
control plane registers them in the **artifact registry**; rows never cross
back into the control plane. Artifacts are tagged with **governed tags**
(`artifact/promote`), and a flow leg can record its input as a new **version**
of a named dataset.

## The builtin engine

The builtin dataflow pack (`cairn-operator-dataflow`) provides the `builtin`
engine for both `kind: data` and `kind: ml` flows, the conformance stages
`noop` and `noop.slow`, and the operators `dataflow/run`, `ml/run` and
`artifact/promote`.

```toml
# examples/hello-cairn/profiles/hello.toml
[profile.backends]
dataflow_engine = "builtin"
ml_engine       = "builtin"
```

Before the leg is submitted, the flow gate checks the flow's **aggregate stage
risk**, the way the approval gate checks a single operator.

## The artifact store

`storage/put` and `storage/get` (`cairn-operator-artifact-local`) write and
read bytes on the engine's disk under `CAIRN_ARTIFACTS_DIR` (default
`~/.cairn/artifacts`). A profile binds the capability:

```toml
# examples/hello-cairn/profiles/hello.toml
[profile.bindings]
"storage.put"    = "storage/put"     # cairn-operator-artifact-local
"storage.get"    = "storage/get"     # cairn-operator-artifact-local
```

```yaml
# examples/hello-cairn/pipelines/profile-and-publish.yaml
- id: stage
  label: Stage the redacted summary
  task: storage/put
  depends_on: [engine-check]
  with:
    store: hello-staging
    key: summary
    content: $steps.redact.text
    content_type: text/plain
```

The leg's output carries the written `uri`, which later legs and the approval
card read (`stage.uri`).

## Promote

`artifact/promote` points a governed tag at a registered artifact. It declares
`requires_approval`, so the tag moves only after a person approves, on the
leg's one run. Its output names the artifact the tag pointed at before
(`previous_artifact_id`), so a rollback has an explicit target.

```yaml
# examples/hello-cairn/pipelines/profile-and-publish.yaml
- id: promote
  label: Tag the checked dataset
  task: artifact/promote
  depends_on: [review]
  with:
    tag: $input.tag                     # default: hello-sample@published
    artifact_id: '$jmes:steps."engine-check".findings.artifact_ids[0]'
    reason: "profile passed the data gate and a person approved the summary"
```

A flow leg's stats sit under `findings`, and `$steps.<id>.<field>` has no
indexing, so this wire uses the `$jmes:` form. An ml leg that produces a model
exposes it as `candidate_artifact_id` instead
(`artifact_id: $steps.train.candidate_artifact_id` in `tiny-finetune`).

## Dataset versions

A `noop` stage given `register` and `source` registers the source file's
content as a version of the named dataset. A changed file is a new version;
the same file adds none. This is what a `dataset` watch looks at (see
[Triggers and events](/features/triggers-and-events/)).

```yaml
# examples/hello-cairn/flows/engine-check.yaml
name: engine-check
kind: data
stages:
  - stage: noop
    config:
      register: $config.register
      source: $config.source
```

```yaml
# the leg in profile-and-publish.yaml binds both keys
- id: engine-check
  flow: engine-check
  depends_on: [redact]
  with:
    source: $input.path
    register: hello-sample
```

## Setup

All of it ships with the engine's builtin packs. Set `CAIRN_ARTIFACTS_DIR` to
move the local store. Inspect what a pipeline produced with
`cairn remote findings <pipeline_id>`.

## Limits

- The public stages transform nothing; they prove the path (engine selection,
  flow gate, a leg run, a registered artifact).
- `storage/get` is bound in the example profile but no example leg calls it.
- The Metaflow engine is its own optional wheel: `pip install 'cairndev[metaflow]'`, then set
  `[profile.backends] ml_engine = "metaflow"` (or `dataflow_engine`). Selecting it without the
  wheel is refused at launch, naming the extra. SkyPilot is the same: `cairndev[skypilot]`.
  No example pipeline runs either.
- An S3 relay for artifact bytes (`CAIRN_ARTIFACT_STORE=s3`) exists in the
  engine but is not covered by the example or the public tests.
- Today the dataset version is registered by the flow leg that checks the
  file, so with a `dataset` watch on, a run that registers a new version is
  followed by one watch-started run on that same content.
