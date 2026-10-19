---
title: Python SDK
description: Author pipelines as Python classes that compile to cairn records — governed, hashed, and drift-pinned.
---

`cairn-sdk` is an **authoring projection**: you write a pipeline as a Python
class, and `cairn pack build` compiles it into the same `pipelines/*.yaml` +
`flows/*.yaml` records a hand-written pack ships. The runtime never imports
your class — the compiled record is what installs, hashes, gates, and runs.
After build, an SDK-authored pack is indistinguishable from a hand-written
one.

Two rules carry the whole design:

- **Every word maps 1:1 to a record.** The class is a view; the YAML is the
  artifact of truth. Rebuilding an unchanged class is byte-identical.
- **A `def` runs, an assignment declares.** Assignments reference existing
  runnables; a decorated `def` becomes real code — lifted into a private
  operator in your pack's wheel at build time.

```python
from cairn_sdk import inp, parallel, pipeline, step

@pipeline(series="support-model", pack="support-model")
class SupportFinetune:
    """Readiness-gated fine-tune; the eval and the adapter record run in parallel."""

    class Inputs:                       # → run form + API schema
        base_model: str = "Qwen/Qwen2.5-0.5B"
        expectation: dict = {"kind": "rows", "roles": [{"name": "text", "required": True}]}

    @step(requires=["connections", "artifacts"], risk="read_only", retries=3)
    def ingest(self, ctx):              # a def RUNS: lifted into a private op
        raw = ctx.connections["tickets"].export(since="last_run")
        return {"dataset": ctx.artifacts.put("raw.jsonl", raw)}

    profile = step("data/profile",      # slash = shared op (task leg)
                   source=ingest.out.dataset, expectation=inp.expectation)
    check = step("check",               # no slash = this pack's own flow
                 path=ingest.out.dataset)
    train = step("train/lora", dataset=ingest.out.dataset,
                 base_model=inp.base_model, timeout="2h", retries=2)
    evaluate = step("train/eval-loss", adapter_path=train.out.adapter_path,
                    base_model=inp.base_model, dataset=ingest.out.dataset)
    record = step("storage/put", store="adapters", key="support",   # the pack declares
                  content=train.out.adapter_path)   # [requires.artifacts.adapters]; this records
                                                    # where the adapter is, not its weights

    graph = ingest >> profile >> check >> train >> parallel(evaluate, record)
```

```bash
cairn pack build support_finetune.py:SupportFinetune
```

The build prints every file it wrote plus the record's **content hash** — the
pin that makes `run()` drift-safe (below).

## The vocabulary

| Word | Means | Compiles to |
|---|---|---|
| `pipeline` | the class decorator: series, budget, approvers, notify, pack name | the pipeline record header + `governance:` |
| `step("pack/op")` | USE a shared operator | a `task` leg |
| `step("name")` | USE this pack's own flow | a `flow` leg |
| `@step(...)` on a `def` | MAKE a private op from the body | a `task` leg + generated operator code |
| `flow.data` / `flow.ml` | MAKE a flow inline with `.stage(...)` | `flows/<name>.yaml` + a `flow` leg |
| `flow.agentic` | an agentic leg (the body lives in `flows/<name>.md`) | a `template` leg |
| `gate(approvers=...)` | STOP for a human — durable HITL | a `gate` leg |
| `gate(prep.out.rows >= 1000)` | STOP answered by DATA — instant, fail-closed | a `gate` leg with `predicate:` |
| `when(pred, target)` | ROUTE — false skips the leg, the run continues | `when:` on the leg |
| `loop(..., until=..., max_iterations=N)` | the only cycle, bounded at parse | a `loop` block |
| `parallel(a, b)` | concurrent chain position | fan-out/fan-in `depends_on` |
| `inp.x` / `step.out.y` | data references | `$input.*` / `$steps.*` wires |
| `cron` / `watch` | entry triggers | captured as build notes (see honesty section) |

Data references are the **authoritative ordering** (D4): if `profile` consumes
`ingest.out.dataset`, that edge exists whether or not `graph` mentions it. The
`graph` chain may only *add* control edges — a chain that contradicts or omits
a data edge is a compile error, never a silent reorder. The same rule applies
inside `parallel`: two steps grouped as concurrent may not have a data edge
between each other.

`retries=N` and `timeout=` compile onto the leg's retry/timeout fields — the
engine executes them; the SDK ships no retry machinery of its own.

## Lifted defs and capabilities

A `def` decorated with `@step` is lifted at build into a private operator with
ref `<pack>/_steps/<pipeline>.<step>`, registered in your pack's wheel like
any operator. Its capability surface is **fail-closed**:

- `requires=[...]` names every `ctx.*` capability the body touches
  (`connections`, `artifacts`, `secrets`, ...). It is **mandatory** the moment
  the body uses ctx — the build refuses to compile without it.
- The build lints the body's AST and names any `ctx.X` use missing from
  `requires`. The lint assists your declaration; it never infers it.
- At **runtime**, the context handed to a lifted op is guarded: any access
  outside the declared list — including dynamic access no lint can see —
  raises `CapabilityNotDeclared`.
- Omitted `risk` defaults to `external_write`, the conservative operator
  default; policy and approval routing read it like any operator risk.

Ambient context (`ctx.logger`, identity fields, heartbeat/cancel helpers)
needs no declaration.

The build also writes the wheel wiring — `pack.toml` operator tables,
`pyproject.toml` entry points, and a test scaffold in the pack's own `tests/`
— but **only for files that don't exist yet**. Your hand-maintained
`pack.toml` or `pyproject.toml` is never overwritten; the build prints a note
with the entries it needs instead.

## Running by reference, pinned against drift

`run_pipeline` never executes your class. It submits the *installed record* by
ref, pinned to the hash your build produced — if someone changed the installed
pack since you built, the server refuses with a 409 instead of silently
running different steps than the class you imported.

```python
from cairn_client import CairnClient
from cairn_sdk import compile_pipeline, run_pipeline

compiled = compile_pipeline(SupportFinetune)
client = CairnClient("https://app.example.com", token=...)

run = run_pipeline(client, "support-model/support-finetune",
                   inputs={"base_model": "Qwen/Qwen2.5-0.5B"},
                   compiled=compiled)          # ← the drift pin

result = run.wait(
    timeout_s=2 * 3600,
    on_gate=lambda g: g.approve(approver="ci"),  # unattended CI approval
)
```

`PipelineRun` gives `status`, `steps()`, `pending_gate()`, `cancel()`, and
`wait()`; `wait(on_gate=...)` fires once per newly awaiting gate so CI can
approve programmatically, while a run without the callback simply keeps
polling while a person answers through the CLI or the API.

## Site vocabulary: environments and policies

Platform facts — *where it runs and what's allowed* — live in their own file,
never imported by pipeline code:

```python
from cairn_sdk import Budget, Connection, Environment, Policy

prod = Environment(
    "prod",
    backends={"storage": "postgres",
              "artifact_store": Connection.ref("s3-prod")},
    policies=[
        Policy.approval(on_risk="external_write",
                        approvers=["ml-leads", "slack:#ml-runs"]),
        Policy.require_gate(verdict="cost", before="train/*"),
    ],
    budget=Budget(per_run_usd=40, per_series_usd=400),
)
prod.compile().write("site/")   # → profiles/prod.toml + policies/*.yaml
```

`Policy.require_gate(before="train/*")` compiles to a policy that injects
an approval gate **at plan time** before every matching pipeline leg — the
gate is not in the author's record and cannot be removed there.

## What the SDK refuses to fake

The vocabulary is closed: a construct the records cannot hold becomes a loud
compile error or an explicit build note — never a silently invented semantic.

- **Pipeline-level `on = [cron(...), watch(...)]`** compiles to a build note
  pointing at cairn's schedule and watch-trigger stores, which own that
  wiring today.
- **`Budget` / `models=` on an Environment** compile to build notes naming
  where those really live (run/series budgets; the model-role registry) —
  budgets and role maps are live state, not installable records.
- **Async lifted defs** and **lifted defs inside `loop(...)`** are compile
  errors, not degraded approximations.
