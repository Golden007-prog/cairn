---
title: Governance
description: Audit, budgets, redaction and policies — the machinery every leg passes through, which a pipeline can tighten and never loosen.
---

Governance in cairn is engine machinery, not a prompt. Four pieces apply to
every leg:

| Piece | What it does | Where it fires |
|---|---|---|
| **Policies** | add a gate, require a dry run, or deny | at the launch door (plan time) and on every operator call |
| **Budget** | reserves a pipeline's ceiling at launch, settles it at the end; costs every model call | launch, every model call, every terminal |
| **Redaction** | rewrites text before it is stored or sent to a model | a `redact/text` leg, and the model-call chokepoint when bound |
| **Audit** | an append-only, hash-chained log | an `audit/store` leg, and the `audit.sink` binding |

## Policies

A policy is YAML: an `applies_to` filter and rules. The strictest matching
effect wins: `deny` > `require_hitl` > `require_dry_run` > `allow_auto`. When
no policy matches, an environment of the `production` tier requires a person
and other environments run automatically.

```yaml
# examples/hello-cairn/policies/require-review-on-publish.yaml
name: hello-require-review-on-publish
description: In the hello-prod environment, a storage write runs only after a person approves it.

applies_to:
  action_types:
    - storage/put
  environments:
    - hello-prod

rules:
  - effect: require_hitl
    reason: "a write in hello-prod needs a person's approval (hello-cairn policy)"
    approval_role: data-owners
```

Launch `profile-and-publish` with `--environment hello-prod` and the `stage`
leg stops for an approval before it writes, although the pipeline put no gate
there.

Where policies come from:

- **pack policies** — a pack's `policies/` directory. They are proposals: an
  environment adopts all of them unless its profile lists the ones it takes
  (`pack_policies = [...]`);
- **an overlay directory** — `~/.cairn/policies`, or `CAIRN_POLICIES_DIR`.

A policy whose `applies_to.pipeline_legs` globs match a leg fires **at plan
time**, before any record exists: `require_hitl` inserts an approval gate in
front of the leg (the step carries `injected_by=<policy>`), and `deny` refuses
the launch and names the policy.

:::caution[Scope a pack policy by environment name]
An unknown environment name resolves to the `production` tier. A pack policy
scoped to the `production` tier would therefore govern every named
environment. The example scopes its policy to the name `hello-prod` for this
reason.
:::

## Budget

A pipeline declares its ceiling under `governance:`. It is reserved at launch
and settled when the pipeline ends, and it shows on the record.

```yaml
# examples/hello-cairn/pipelines/ask-the-docs.yaml
governance:
  # One small model call; the reservation is the ceiling, not the expected spend.
  budget: {usd: 0.5}
  series: hello
  approvers: data-owners
```

Every model call built by the engine carries a cost callback, so each call's
cost lands on its run. The proof shows the budget **declared and recorded**;
it does not drive a pipeline over its ceiling.

## Redaction

As a leg, `redact/text` (`cairn-operator-redact-regex`) replaces each hit of
the builtin regex recognisers (emails, phone numbers, keys, and so on) with a
placeholder and reports `span_count`:

```yaml
# examples/hello-cairn/pipelines/profile-and-publish.yaml
- id: redact
  label: Redact the profile summary
  task: redact/text
  depends_on: [data-ok]
  with:
    text: $steps.profile.note
```

At the model-call chokepoint, redaction is on when the active profile binds
`redact.text`. Every model call is built by one factory, so a bound redactor
sees every prompt and every tool result before it leaves the engine:

```toml
[profile.bindings]
"redact.text" = "redact/text"     # as src/cairn/profiles/secure.toml binds it
```

With no binding, model calls pass through unchanged.

## Audit

`audit/store` (`cairn-operator-audit-store`) appends a row to a hash-chained
JSONL file: each row's hash is computed from the previous row's hash and the
event, so an edit breaks the chain. The file is `CAIRN_AUDIT_LOG` (default
`./audit.jsonl`).

```yaml
# examples/hello-cairn/pipelines/profile-and-publish.yaml
- id: audit
  label: Record the publication
  task: audit/store
  depends_on: [publish]
  with:
    action: hello.dataset.published
    outcome: ok
    actor: data-owners
    target: $steps.publish.uri
```

Binding `audit.sink` to `audit/store` in the profile (as `secure.toml` does)
sends the engine's own audit events to the same sink.

## Setup

All four ship with the engine and its builtin packs. Point the engine at a
profile that binds what you want on (`CAIRN_PROFILE`), set `CAIRN_AUDIT_LOG`,
and drop policies in a pack or in the overlay directory.

## Limits

- Redaction on the example's model call is **not** shown end to end: the
  example profile does not bind `redact.text`. The `redact/text` leg runs in
  `profile-and-publish`.
- The Presidio-backed redactor (the `pii` extra) is not part of what the
  example proves.
- `approvers:` is recorded, not enforced (see [Gates](/features/gates/)).
