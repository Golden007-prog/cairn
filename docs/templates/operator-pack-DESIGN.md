<!-- Template: cairn operator-pack design record. Copy to
     cairn/operators/<tier>/<pack>/DESIGN.md and fill EVERY section.
     Mandatory per CLAUDE.md rule 8 and gated by
     tests/core/test_pack_design_docs.py — a change to the pack's code
     lands in the same PR as the change to this file. Delete the comments. -->

# <pack-name> — design

- **Distribution:** `<pip name>` (`cairn/operators/<tier>/<pack>/`)
- **Capabilities provided:** `<capability.one>`, `<capability.two>`
- **Status:** proven | experimental | held
- **Decisions:** ADR-xxxx, #issue

## 1. Purpose

What problem this pack exists to solve, in one paragraph, from the caller's
side. What it deliberately does NOT do.

## 2. Architecture

How it works: what it wraps (library, service, CLI), where the work runs
(in-process, remote job, external API), where state lives, and the data flow
through the pack. Name the third-party dependency and why it, rather than the
alternatives that were rejected.

```
<a short diagram or step list: input -> stage -> stage -> output>
```

Concurrency, idempotency and retry behaviour. What happens on a partial
failure.

## 3. Operators

| Operator | Capability | Risk | Mock | Input -> Output | Reports (stages · series · scalars) |
|---|---|---|---|---|---|
| `<pack>/<op>` | `<capability>` | read_only / external_write / paid_compute | yes/no | `<InputSchema>` -> `<OutputSchema>` | `—` or `stage1, stage2 · series_a (unit), series_b · scalar_x` |

One subsection per operator when the contract needs more than a row: the
fields that matter, the invariants, and what the operator refuses.

**Reports** (ADR-0103) is the operator's `reports = ReportSpec(...)` declaration,
mirrored in `pack.toml` and compared by the manifest gates: every declared series
and scalar name must appear in this record (the design-docs gate checks). `—`
means `ReportSpec.NONE`, which must be stated on the class, never left out.

## 4. Configuration

| Setting | Where | Scope | Default | What it does |
|---|---|---|---|---|
| `CAIRN_<VAR>` | env | deployment | `<value>` | |
| `<field>` | `input_schema` | run | — | per-run bound; a caller may set it |
| `<field>` | `config_schema` | governance | `<value>` | ENV-OWNED: a run-supplied value is DROPPED (ADR-0068/0070) |

**Per-run bounds belong in `input_schema`.** A ceiling declared in a
governance-scoped `config_schema` field is silently discarded at run time, so
the pack advertises a limit it does not enforce (#905). State explicitly which
side each knob is on.

Connections required (`kind:`), secrets consumed, and what the pack does when
they are absent (fail closed, or degrade and say so).

## 5. Governance

Risk class per operator and why. Which gates a caller is expected to place
around it. What is auditable (what lands in the hash chain) and what a
deployment policy can override.

## 6. Failure modes

| Symptom | Cause | What the operator returns | Fail closed? |
|---|---|---|---|

Include the ones found in production, with the issue number.

## 7. Testing

Where the pack's tests live (`tests/` inside the pack, CLAUDE.md rule 5), what
is covered by golden fixtures, and what needs a live service or a GPU (and so
does not run in CI). How to run them from a worktree.

## 8. History

| Date | Change | PR |
|---|---|---|
