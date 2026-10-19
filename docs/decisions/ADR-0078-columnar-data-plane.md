# ADR-0078 — Columnar data plane: Arrow in memory, Parquet at rest

Status: Proposed (2026-07-18)

Relates to / builds on: ADR-0026 (pack contract — everything here lands inside
operator packs, no engine change), ADR-0047 (run provenance — Parquet evidence
artifacts hash into the audit chain), ADR-0076 (loop-aware core — the D3 fix loop
is the first consumer of staged datasets; D4 watch re-runs cheaply off a staged
profile), #962 (Shardable core — the process-pool fan-out this rides), #964
(batched Presidio), #654/#657 (Arrow-native correlation — the RCA primitives this
generalizes), #321 (readiness perf benchmark). Companion analysis: the
massive-dataset gap audit (2026-07-18) that produced this ADR.

## Context — the verified gaps this closes

Two workflow families hit the same wall: **cairn's algorithms scale, but its data
plane doesn't.** Every claim below is code-verified (file:line).

### Fine-tuning dataset prep (`operator-dataset-checks`)

- **Every stage re-parses JSONL from scratch.** All outputs are JSONL
  (`JsonlWriter`/`ShardedJsonlWriter`, `_io.py:146-235`); a check→fix→anonymize→
  split pipeline pays full JSON parse cost four times. The columnar fast path
  (`scan_frame` → polars `LazyFrame`, `_io.py:304-330`) **exists but no operator
  calls it** — dead code relative to its own docstring (#381/#382).
- **Zero durability.** No manifest, checkpoint, or atomic rename anywhere in the
  pack. `run_sharded` holds shard partials in memory and `rmtree`s its workdir in
  `finally` (`sharded.py:160-187`); writers `open(..., "w")` in place — a crash
  mid-write leaves a **truncated output**, and a crash at 90% of a 10M-row run
  re-runs 100%.
- **One malformed row is fatal.** `_iter_jsonl` calls `json.loads` with no
  try/except (`_io.py:293-301`). No quarantine sink exists.
- **Only the detect half scales.** 4 ops are Shardable (dedup, decontam,
  pii_scan, anonymize); the remediation ops the ADR-0076 fix loop depends on —
  `dedup_apply`, `decontam_apply` — plus profile, sample, format, curate all
  `load_rows()` the whole file into RAM.
- **Split is not dedup-cluster-aware.** `dataset/split` guarantees `group_by`
  cohesion (`_split.py:322-365`) but knows nothing about the MinHash clusters
  `dataset/dedup` already computes — near-duplicates can straddle train/val,
  the classic eval-leakage failure.

### Incident RCA (`clickstack` backend + correlation)

- **Raw rows into Python.** Every probe is `SELECT … LIMIT n` materialized as
  `list[dict]` via per-line `json.loads` over a fully-buffered HTTP body
  (`backend.py:83-108`). `search_traces` is N+1 (one query per trace,
  `backend.py:375-417`). Aggregation is pushed down only for topology
  (`backend.py:560-598`).
- **The Arrow migration is ~10% done.** `_query_arrow` (ClickHouse
  `FORMAT ArrowStream` → `polars.read_ipc_stream`, `backend.py:111-147`) and the
  parity-tested DataFrame reductions (`correlate.py:237-270`) exist, but only
  blast-radius error counting consumes them; `git/correlate` still pulls 500
  error dicts (`operator_git/operators/correlate.py:122`).
- **No condensation before the LLM.** The synthesize prompt receives raw
  `state.findings` lists; the query `LIMIT`s (100 logs / 20 traces / 100
  metrics) are the *only* context bound. No clustering, top-k, or truncation
  exists between telemetry and the model. Wide-window RCA (the stated goal of
  the Arrow work, `correlate.py:16-20`) is impossible under this shape.

## Decision

Adopt a two-layer columnar data plane, implemented **entirely inside operator
packs and backend adapters** (no engine change), with the hermetic-fallback
rule every cairn extra follows:

1. **Arrow is the in-memory contract for hot paths.** Operators and backend
   probes exchange Arrow-backed frames/record-batches (via **polars**, already
   the pinned `[fast]` extra in both the dataset pack and the clickstack
   backend) instead of `list[dict]`. The pure-Python dict path remains the
   base-install fallback, parity-tested against the columnar path — the exact
   pattern `correlate.py` already established (`_pl_parsed`, byte-for-byte
   reconciliation).
2. **Parquet is the at-rest format for pipeline stages and evidence.**
   Multi-step dataset pipelines stage shards as Parquet with a manifest and
   atomic commits; RCA persists its correlation inputs as Parquet evidence
   artifacts hashed into the audit chain. **JSONL stays at the edges** — ingest
   accepts it, and final export always emits trainer-ready JSONL (HF/OpenAI
   formats), because JSONL is the ecosystem's interchange format, not our
   internal one.

## Design A — staged dataset pipeline (Parquet between stages)

```
raw.jsonl ──► 01-ingest ──► 02-dedup ──► 03-pii ──► 04-split ──► export.jsonl
              shard-*.parquet each, + manifest.json, + quarantine/*.parquet
```

**A1 — Stage store + durability (the core).** A small `StageWriter` in the
dataset pack (not the engine): each stage writes
`<out>/<stage>/shard-NNNN.parquet` via tmp-file + `os.replace`, then records the
shard in `<stage>/manifest.json` (shard id, rows in/out, checksum, config hash).
Idempotent resume = skip shards whose manifest entry matches the config hash;
a crashed run re-does only the in-flight shard. Malformed rows stop being fatal:
the ingest stage catches `JSONDecodeError` per line and writes
`quarantine/<stage>.parquet` (record id, error code, source line) — surfaced as
a count on the readiness report instead of a dead run. This is the
shard-as-unit-of-work / atomic-commit pattern, scoped to one pack.

**A2 — Arrow batch contract for Shardable.** `run_shard` gains a columnar
variant: workers open the staged Parquet with `pl.scan_parquet`, slice their
`[start, end)` range, process as record batches, and write **their own** output
shard — eliminating today's in-memory partials + concat merge
(`batch.py:135-141`). This finally wires `scan_frame` and removes the
crashed-child-fails-everything failure mode.

**A3 — Migrate the materializing ops.** `dedup_apply` / `decontam_apply` become
one-pass batch filters over the stage scan (the flags are already computed —
applying them never needed the whole file in RAM); profile / sample / format /
curate move to batch iteration. Vectorized wins where `[fast]` is present:
`canonical_hash`, token counts, and shingles as polars expressions, dict
fallback kept and parity-tested.

**A4 — Cluster-aware split.** `dataset/dedup` writes its cluster verdict as a
`dedup_cluster_id` **column** in the staged Parquet; `dataset/split` treats it
as an implicit `group_by`. Closes the train/val leakage hole with zero new
algorithms — the clusters and the group-aware splitter both already exist;
Parquet is the missing wire between them.

**A5 — Dataset identity: fingerprint + gate-to-train handoff.** Two verified
holes in the training path that the stage store closes almost for free:

- **No content fingerprint exists.** The only dataset hash in the training pack
  is `sha256(uri)` used as a *download cache key*
  (`operator_training/dataset_fetch.py:35`) — never the content. The model card
  records `dataset_uri` + readiness verdict + `dataset_run_id`
  (`model_registry/schemas/card.py:20-35`), so lineage pins *which check
  passed* but not *which bytes trained*. Edit the JSONL in place after the
  check and provenance still says PASS. Fix: A1's manifest already records a
  checksum per shard — the **dataset fingerprint is the hash of the ordered
  shard hashes**, emitted by every stage and threaded into `TrainSpec` and the
  model card. Reproducibility becomes content-addressed, matching how
  templates already work (ADR-0047 `schema_hash`).
- **The artifact that passes the gate is not the artifact that trains.** Base
  `finetune` runs `dataset-readiness/check` and then trains on the **raw**
  `dataset_path` (`packs/training/templates/finetune.md:140-141`); the D3 fix
  pipeline's output only trains if a user manually rewires it. The S3 staging
  leg (#701) uploads the raw file unchanged. With A1, the gate's output *is* a
  staged, fingerprinted artifact ref — `finetune` consumes that ref, so
  "checked", "fixed", and "trained-on" are provably the same bytes. HF
  `datasets` loads Parquet natively, so the staged shards feed the trainer
  directly; JSONL export remains only for external trainer APIs.

### Why no ClickHouse/DuckDB analogue for datasets (the asymmetry)

Telemetry and training data are opposite workloads. OTel data is an unbounded,
append-only, high-cardinality **stream** queried concurrently by aggregation —
that is a database server's job, and ClickHouse keeps it. A training dataset is
a **bounded, versioned artifact** processed in discrete single-writer passes
(profile → dedup → PII → split) — that is a file-format + library job. The
dataset side's needs are columnar scan/aggregate/join (polars, at the 10M-row
single-box scale the pack targets), row-wise NLP fan-out (the Shardable pool),
durable staging (A1), and identity (A5) — none of which wants an always-on
server, which the pack contract's no-mandatory-infra rule would forbid anyway.

## Design B — columnar RCA (Arrow on the wire, aggregates over rows)

**B1 — ArrowStream as the default transport.** Generalize `_query_arrow` so the
log/trace/metric probes return polars frames when available (`get_*_arrow`
variants, dict fallback preserved for mock/otel). Replace the trace N+1 with one
`WHERE TraceId IN (...)` query. Move `git/correlate` onto the already-built
`error_onset_df` path — pure wiring.

**B2 — Pushdown aggregation probes (the LLM-context fix).** New backend methods
that make ClickHouse do what it's for: error counts per time-bucket per service,
and **top-K normalized log templates with counts** (message fingerprinting
server-side where ClickHouse functions suffice; client-side template mining over
an Arrow column behind an extra where they don't). Synthesize then receives
*"12 templates, ranked, with counts and first/last seen"* instead of *"the 100
newest raw lines"* — the context bound becomes semantic rather than an arbitrary
`LIMIT`, and widening the time window from 1h to 24h changes the counts, not the
payload size.

**B3 — Parquet evidence artifacts.** The frames feeding a correlation verdict
(error series, deploy list, template table) persist as Parquet artifacts
hash-chained into the run audit (ADR-0047). An RCA becomes *reproducible* — the
verdict can be re-derived from the exact evidence — and wide-window RCA becomes
memory-bounded because only aggregates ever cross the wire.

## Problems solved (problem → mechanism)

| Verified problem | Mechanism | Phase |
|---|---|---|
| Truncated outputs / whole-run re-run on crash | Parquet shards, tmp+rename, manifest resume | A1 |
| One malformed row kills a 10M-row run | Per-row quarantine Parquet at ingest | A1 |
| Every stage re-parses JSONL | Parse once at ingest; typed Parquet between stages | A1 |
| Crashed shard child fails whole map; partials in RAM | Workers commit own Parquet shards | A2 |
| `scan_frame` dead code | Becomes the standard stage-read path | A2 |
| Fix/apply ops OOM on files the check streamed | One-pass Arrow batch filters | A3 |
| Near-dups straddle train/val | `dedup_cluster_id` column → implicit `group_by` | A4 |
| No dataset content hash — lineage pins the check, not the bytes | Fingerprint = hash of shard hashes → `TrainSpec` + model card | A5 |
| Gate passes on checked data, training runs on raw `dataset_path` | Gate emits staged fingerprinted ref; `finetune` consumes it | A5 |
| RCA pulls raw `list[dict]`, buffered bodies, N+1 traces | ArrowStream transport everywhere + IN-query | B1 |
| LLM sees arbitrary newest-100 lines; window widening impossible | Pushdown time-bucket + top-K template aggregates | B2 |
| RCA verdicts not re-derivable; wide windows unbounded | Hash-chained Parquet evidence artifacts | B3 |

## What is BUILT vs WIRED

**Wire (exists today):** polars `[fast]` extra (both packs), `scan_frame`,
`_query_arrow` + `read_ipc_stream`, `window_error_count_df`/`error_onset_df`
(parity-tested), `Shardable`/`ShardSpec`/process pool, MinHash clustering,
group-aware split, batched Presidio.

**Build (net-new, all pack-local):** `StageWriter` + manifest + quarantine (A1);
columnar `run_shard` variant (A2); batch migrations of 6 ops (A3);
`dedup_cluster_id` plumbing (A4); `get_*_arrow` probe variants + IN-query (B1);
aggregation probe methods + template mining (B2); evidence-artifact hook (B3).

**Not built:** no DuckDB (polars expressions/scan cover every identified need —
adding a second columnar engine is surface without capability), no pyarrow
direct dependency (polars vendors its own Arrow/Parquet IO), no Ray/distributed
tier (A1's manifest is the substrate the future #962 isolated worker would
dispatch over, but that stays deferred), no new engine concepts.

## Non-goals

- **JSONL is not deprecated.** It remains the public ingest and export format;
  Parquet is internal staging only.
- **Base installs stay hermetic.** Without `[fast]`, everything runs on the
  dict paths at today's scale ceiling; columnar paths are acceleration +
  durability, never a hard dependency. Requesting a staged/columnar mode
  without the extra raises the same clear `RuntimeError` pattern as
  `[minhash]`.
- **Not a lakehouse.** No catalogs, no table formats (Iceberg/Delta), no
  cross-run dataset registry — a stage dir belongs to one run.

## Phasing

Each phase is independently shippable and maps to one issue: **A1** (durability
core — highest value, unblocks everything), **A2**, **A3**, **A4**, **A5**
(fingerprint + gate-to-train handoff; depends only on A1's manifest) on the
dataset side; **B1** (wiring, cheap), **B2** (the RCA quality jump), **B3** on
the RCA side. A1 and B1 have no interdependency and can land in either order.

## Open questions

1. Stage-dir ownership: per-run scratch under the run's artifact dir, or a
   user-supplied `staging_dir` input on the pipeline templates?
2. Template mining in B2: are ClickHouse-native normalization functions enough,
   or do we adopt a miner (e.g. drain3, MIT, pip-only) behind an extra?
   (Verify license + no-infra at adoption, per `/verify-upstream`.)
3. Should the readiness *report* schema surface quarantine counts as a first-
   class verdict dimension (it changes `DataValidation`, ADR-0074)?
4. Evidence-artifact retention: RCA Parquet evidence can contain sensitive log
   bodies — same handling question as ADR-0077's approve-publish gate.

## Status / next step

Proposed — design only, no code. Promote to Accepted + open an epic
(A1 → B1 → A3 → B2 → A2 → A4 → B3 suggested order) when prioritized.
