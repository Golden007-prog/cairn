---
title: Configuration
description: Operator config scoping (governance vs operational), precedence, input presets, and the side-effect-free checks before a launch.
---

Operators declare their tunable knobs in a `config_schema`; cairn layers the
values and enforces **who may set what**.

## Two scopes

Every knob has a scope, tagged in the schema:

- **governance** — safety-relevant thresholds (gate limits, spend caps). Owned
  by the **environment**; a workflow or run that tries to set one is rejected
  at preflight.
- **operational** — tuning (batch sizes, sample counts, model choices).
  Settable per workflow and per run. Untagged fields default to operational.

## Precedence

The effective value of an operational knob resolves as:

```
run override > run preset override > default preset > workflow config > environment profile > operator default
```

Governance knobs skip the middle: environment profile > operator default,
always. Explicitly wired inputs always beat config — config fills gaps, it
never overrides the graph.

Per-operator config in a profile lives in `[profile.config."<operator>"]`
tables; see [Profiles](/concepts/profiles-environment/).

## Inspecting the surface

```bash
cairn config workflow hello-cairn/assistant          # every reachable operator's knobs
cairn config workflow hello-cairn/assistant --json   # grouped, governance vs operational
```

For a pipeline, `cairn pipeline explain <ref>` shows each leg's config values
with the layer each one came from; `-i key=value` shows what a launch input
would change.

## Input presets

An **input preset** is a saved set of launch inputs for a pipeline:

```bash
cairn remote launch hello-cairn/profile-and-publish -i min_rows=15 --save-preset strict
cairn remote launch hello-cairn/profile-and-publish --preset strict
cairn remote presets hello-cairn/profile-and-publish
```

A preset is saved only from a launch the server accepted, so it is always a
set of inputs the door already took. `--input` overrides a preset per key.

## Checks before a launch

```bash
cairn pipeline explain hello-cairn/tiny-finetune -e sandbox   # a pipeline: legs, gates, engines, config
cairn preflight hello-cairn/assistant                          # a template: exit 0 ready, 1 blocked
cairn validate examples/hello-cairn                            # a pack, offline
```

The preflight verdict reports:

- **blockers** — missing inputs, unresolved operator bindings, governance
  config violations (the same fail-closed check the dispatcher enforces),
  template not found;
- **gates** — which steps will pause for approval (a heads-up, not a
  blocker);
- **cost** — the estimated spend;
- **resolved_config** — the effective layered config it would run with;
- **warnings** — advisory only, e.g. unhealthy connections (never blocks).

`cairn run --dry-run` additionally prints the compiled node DAG of a flow
without running it.
