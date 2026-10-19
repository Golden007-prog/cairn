---
title: Cost and budgets
description: Cap what a pipeline may spend, reserve a flow leg's estimate before it starts, stop on a price with a data gate, and read what every run cost.
---

cairn treats cost as something you decide on before the money is spent, and
check afterwards. Three mechanisms work together: a budget the pipeline
declares, a reservation every flow leg takes against it before it starts, and
data gates that stop on a number an earlier leg produced.

## Budgets on the pipeline

A pipeline declares its budget in `governance`:

```yaml
governance:
  budget: {usd: 25}
  series: nightly-batch
  estimated_usd: 4
  approvers: data-team
```

- `budget.usd` is the pipeline's spending cap.
- `series` names a group of runs that share that cap.
- Before a flow leg starts its engine, it **reserves** the pipeline's
  `estimated_usd` against the series cap. The reservation is atomic (a lock
  per series on Postgres), so two legs cannot both take the last dollar.

When a reservation would go over the cap, the leg fails before it spends, with
this message (values filled in):

```text
series '<series>': this run's estimate $<estimate> would exceed the $<cap> cap
($<spent> spent, $<held> reserved by in-flight runs). Raise the budget or start a new series.
```

A finished leg settles its reservation. A failed or cancelled leg releases it.
A retried attempt reserves again, because a retry is new spend.

## Stop on the price: a data gate

cairn does not guess a price for you. Pricing is a step in the pipeline: a leg
whose output carries the number, and a data gate that reads it. With your own
pricing operator or flow stage in a leg called `estimate`:

```yaml
  - gate: cost
    depends_on: [estimate]
    predicate: steps.estimate.usd <= input.max_cost_usd

  - id: run
    flow: batch-job
    depends_on: [cost]
```

When the estimate is above `max_cost_usd`, the gate blocks and the pipeline
stops before the leg that spends. A data gate can't be approved past, so
nobody can wave an over-budget run through. Put a person gate after it if the
spend also needs a person's agreement.

The operators that price model calls and GPU runs (token estimates, GPU
estimates and live GPU quotes, and a threshold gate over them) are part of
cairn-platform.

## What a run cost

Every run records its cost when it finishes, split into model calls, GPU and
storage, plus a total (`llm_usd`, `gpu_usd`, `storage_usd`, `total_usd`).
Model calls are priced as they are made; GPU and storage come from what the
operator reports. The series total is what the next reservation is checked
against.

## Next

- [Gates and approvals](/concepts/gates/): the gates that read these numbers.
- [Runs and the record](/concepts/runs/): where the cost is kept.
- [Pipeline and flow YAML](/reference/yaml/): every `governance` field.
