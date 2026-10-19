# ADR-0090 — Stage-level fan-out: `map:` on a stage (G4)

Status: **PROPOSED** (2026-08-13 — grammar proposal #1290; execution issue
#1360; the cross-stage-DAG variant is deliberately NOT this ADR, see §5).

Relates to / builds on: **ADR-0082 D8** (a construct the vocabulary cannot
express is a grammar proposal — this is one), **ADR-0081** (engine
neutrality — the grammar names no engine), **ADR-0089** (substrate posture —
execution runs inside the shared interpreter, engines supervise),
**ADR-0088** (checkpoint/resume — per-item markers must compose with it).
Prior in-repo art this ADR reuses rather than rebuilds: the Shardable
operator contract + budgeted process pool (#962,
`cairn/core/operator/sharded.py`), the shard-manifest artifact form
(ADR-0081 D2, `cairn/core/dataflow/manifest.py`), and the agentic
`MapBTNode` semantics (`cairn/core/engine/schema.py`).

Design brief with the full verification table:
`docs/ideas/flow-parallelism-map-needs-brief-2026-08-13.md`.

---

## 1. Context

A flow record (`flows/*.yaml`) is a SEQUENTIAL stage list, and the shared
interpreter (`run_dataflow`) is a sequential loop. Verified consequences
(#1290): swapping engines adds supervision, not parallelism — Prefect and
Metaflow wrap the same loop in one deployment-run/step; even the P4 export
has nothing parallel to project. Pipeline-level `parallel()` legs exist;
stage-level fan-out (shard map, per-file processing, parallel eval sweeps)
does not.

Meanwhile the machinery to EXECUTE a fan-out already shipped at the operator
tier: `Shardable` (#962) plans row ranges, dispatches through
`map_shards_local` (a budgeted, nice'd, container-aware process pool with
picklable re-resolve-in-child tasks), and fans in through a declared
`merge`. And a dataset artifact IS a manifest — an ordered `ShardRef` list
with per-shard stats and a content hash. The gap is only that the flow
grammar cannot address any of it.

## 2. Decision — the grammar

One optional block on a `StageRef`:

```yaml
stages:
  - stage: dedup.exact
    map:
      over: shards            # DEFAULT: the input manifest's ShardRef list
      # over: $config.files   # OR an explicit bounded list (per-file, eval sweep)
      as: item                # binding name; the kernel sees $item in config
      max_parallel: 8         # bounded — unbounded fan-out stays a non-goal
      merge: concat           # concat | stats_sum | <kernel-declared merge>
    config: { ... }
```

Semantics (mirroring the agentic `MapBTNode`, so the two kinds stay one
concept): a non-list `over` value wraps to a single-element list; an empty
or missing list runs the merge over zero results; `merge` fans the per-item
results into ONE stage result. The record stays a **linear stage list** —
`gate_split`, content hashes, export, and the conformance suite are
untouched, and the audit stays linear per item (the #1290 requirement).

`map:` is **independent parallel tasks**. It is NOT the coordinated-gang
`resources.workers` (#1366) — a mapped item never rendezvouses with its
siblings. A stage carrying both is refused at validation.

## 3. Execution (the #1360 half)

Three tiers, one grammar; the admission report states which tier a binding
actually provides:

1. **Interpreter fan-out (this ADR ships it — every engine, day one).**
   `run_dataflow` sees `map:` → plans items (manifest shards or config
   list) → dispatches per-item kernel invocations through the EXISTING
   `map_shards_local` pool contract (`max_parallel`-capped) → merges via
   the kernel-declared merge (the `Shardable.merge` contract lifted to the
   kernel tier) or the default `concat` + stats-sum. Parallelism is bounded
   by the one box/pod the flow landed on — stated, not hidden.
2. **Native fan-out (later, opt-in per binding).** Metaflow `foreach` /
   Prefect `.map()` lower the same grammar to one task per item — separate
   compute, real per-item `resources`. Result contract (merge, producer
   outputs) stays the interpreter's, so semantics never fork per engine.
3. **Agentic `map`** is already native (`Send`) — unchanged.

Resume: per-item done markers under the ONE stage
(`stage-03-dedup_exact.item-007.done`) plus content-hash skip via the
manifest (same shard hash → skip), composing with ADR-0088 checkpoints
(the stage's checkpoint fires after the merge, as for any stage). Budget:
`max_seconds` breach mid-fan-out cancels outstanding items before the abort.
Progress: D8a events gain `items_done`/`items_total` — additive keys,
existing pollers unaffected.

## 4. Rejected alternatives

* **Code-generate per-item steps into the engine's native graph now** —
  reopens everything ADR-0089 §3 rejected (content-hash drift, per-engine
  semantic forks, D1). Native lowering arrives later as an optimization
  BEHIND the grammar, not as its implementation.
* **Unbounded / dynamic-width fan-out** — stays a non-goal (#1290): the
  record must remain diffable and the audit bounded.
* **A new pool / scheduler** — `map_shards_local` already exists, budgeted
  and container-aware; building a second one fails the #962 reuse test.

## 5. Explicitly NOT this ADR: cross-stage DAGs (`needs:`)

#1290 records "cross-stage DAGs inside a flow" as a non-goal ("that's what
pipelines are for"). Amending that is a separate ruling with its own issue
(#1362, recommendation: defer until a real flow makes the pipeline-level
alternative demonstrably painful). Nothing in this ADR's grammar or executor
presumes that ruling goes either way.

## 6. Consequences

* Every existing flow parses, executes, and hashes identically — `map:` is
  additive and optional.
* The four motivating cases (shard map, per-file processing, parallel eval
  sweeps, capped fan-out) become expressible with ~4 lines of YAML and run
  on builtin/SSH/Prefect/Metaflow immediately.
* Per-item compute isolation remains honestly OUT of scope until native
  lowering: a mapped stage's `resources` describe the whole stage envelope,
  and the admission report says so.
* The SDK grows a `.stage(..., map=...)` hint in a follow-up (ADR-0087
  projection — authoring sugar over this record grammar, no new semantics).
