# ADR-0074 — Input data validation: a generic `data/profile` pre-flight (roles, not formats)

Status: Proposed — **under discussion** (2026-07-14)
Relates to / builds on: ADR-0073 (the Playground — where this first pays off:
upload-your-own-data), ADR-0072 (dry-run → preflight verdict + "Promote to run",
#797/#800 — the pre-run gate this plugs into), ADR-0047 (run provenance / the
input+output JSON-Schema contract), ADR-0020/0026 (pack contract; packs are
DATA), #109 (`coerce_and_validate` — the envelope schema check every surface
runs), #745 (`dataset/format` acceptance / expectations seam), #616
(`logs_analytics` `traces/analyze` — the field-mapping pattern this generalizes),
#838 (Playground upload + sample). Companion: `design/cairn-playground-v2.html`.

## Context — the problem this closes

A workflow only produces a trustworthy result if the data it's fed matches what
its operators actually read. Today cairn validates the **envelope** — the run's
input *arguments* — against the workflow's input JSON Schema via
`coerce_and_validate` (`cairn/ask/render.py`), fail-closed with a 422. That is
correct and stays. But it has two gaps the Playground exposed:

1. **Envelope ≠ payload.** Many workflows take a **`path`** (a file / HF id /
   URI), not inline records — e.g. `dataset-readiness/check` requires
   `path: string`, `additionalProperties: false`. The envelope check confirms
   "`path` is a string"; it says *nothing* about whether the file behind it has
   the fields the checks will read. The real data validation happens deep inside
   the operators at run time, after the user has already committed to the run.

2. **The Playground upload doesn't fit.** Upload produces **inline sampled rows**
   placed on a guessed array field; for the flagship that resolves to
   `target_trainers` (or `rows`) and the envelope gate correctly rejects it:
   `422 ["'path' is a required property", "{…} is not of type 'string'"]`.
   Upload-your-own-data — half the Playground north star ("on *their* data") —
   is only half-wired.

We need one **generic** answer for validating input data against the selected
workflow, that spans dataset preparation, dataset evaluation, incident-RCA logs,
metrics, OpenTelemetry spans, RAG ingestion — and whatever comes next — without a
bespoke validator per situation.

## The principle — validate **semantic roles**, never a wire format

The wrong frame is "does this file match schema X." The right frame — which
`traces/analyze` already embodies — is:

> A workflow declares the **semantic roles** its operators will read. Validation
> asks: *can I resolve those roles in your data, to the depth each operator
> needs?* — and reports honestly, degrading gracefully.

`traces/analyze` never demands OTLP-proto. It takes a **field mapping with
defaults** (`service_field`, `duration_field`, `span_id_field`, …) and *degrades*
("dependency edges only when `span_id` **and** `parent_span_id` are present").
That is the model we make first-class for every data kind.

**Strict vs. loose is per-role, not per-file**, decided by one test: *can the
operator produce its output without this field?*

| Role is… | On absence | Example |
|---|---|---|
| **Required** — dereferenced unconditionally; no output without it | **BLOCK** (fail-closed) | eval judge needs `answer`; RAG-eval needs `question/contexts/answer`; a trainer needs a chat/completion shape |
| **Feature-gating** — only *enables* a capability | **DEGRADE** — run, skip that feature, say so | span `parent_id` → dependency graph; dataset `metadata_path` → provenance check |
| **Unstructured** — the payload *is* free content | envelope-only (non-empty · parseable · within caps) | RAG documents, free-text logs |

So we expect a specific shape only for training rows, eval rows, spans, and
metrics — and even there it's "resolve these roles," tolerating arbitrary column
names via mapping. We do **not** for logs and RAG ingestion: content is the only
required role.

## Validation tiers — how strict, and when (resolves "semantic vs. structural")

There aren't two choices (semantic vs. structural) but **three layers** that
answer *different* questions. Climb only as high as the data warrants — the
deciding rule is: **escalate with (a) the cost of one bad record, (b) how
*deferred* the failure is, and (c) whether the data crosses a contract boundary**
(feeds a strict consumer, gets persisted, others depend on it).

| Tier | Question | Tool | Required when | Overkill when |
|---|---|---|---|---|
| **1 · Format / parse** | Does it decode into records? | stdlib + the loaders — **no library** | **always** (it's the floor, and free — the loader parses-or-throws; we just surface the error cleanly) | never (but don't reach for a library) |
| **2 · Semantic / roles** | Are the fields my operators read present & typed? | `data/profile` (this ADR) | **the default** — the universal "will it run" gate | never (this is the baseline) |
| **3a · Per-record structural** | Does *every* record match an exact typed schema? | **Pydantic** (already a base dep) | dataset **prep→training** + writes to a typed/contract sink — high, deferred cost of a bad row | RCA logs, RAG docs — strictness here **rejects valid messy input** |
| **3b · Statistical / dataset** | Does the *dataset* meet quality / drift expectations? | **GX / Pandera** (optional extra) | governed **recurring** pipelines with team-authored data-quality / drift contracts | Playground one-shot, RCA, eval spot-checks |

**Per situation:**

- **Incident RCA — tiers 1+2 only** (and 2 stays loose). Telemetry is messy and
  heterogeneous; RCA reasons over partial data (the whole `traces/analyze`
  degrade model). A strict per-record schema would *reject valid inputs* — a net
  loss. **Semantic is enough.**
- **Dataset evaluation — 1+2**, with **optional 3a on the required roles** only
  if the judge is structure-sensitive (multi-turn, fixed reference shape), where
  a malformed row would silently yield a garbage score instead of an error.
- **Dataset preparation (→ fine-tuning) — 1+2+3a required.** The consumer is a
  trainer: unforgiving, and the failure is deferred and expensive (crashes hours
  in, or trains on garbage). cairn **already does 3a** here —
  `dataset/format._validate_row` validates the chat/turns/roles per row. That is
  exactly where a Pydantic model per target-format belongs.
- **RAG ingestion — 1+2**, unstructured (content is the only required role).

**Pydantic vs. GX, decided:** Pydantic (tier 3a) is for *per-record shape* where
a downstream consumer is unforgiving (prep→training, typed sinks) — and cairn's
operator I/O contracts are *already* Pydantic, so this just extends the same idiom
to payload records. GX/Pandera (tier 3b) is for *dataset-level statistical /
drift* contracts on recurring governed pipelines — and it overlaps heavily with
what `dataset_checks` already does built-in (dedup/leakage/PII/format coverage),
so it's only worth adding if users want to author *declarative* suites.

**Consequence — two operators, both reusable in real runs.** These are
`side_effect_free` **operators**, not Playground-only helpers, so the same code
composes as graph nodes: (a) Playground preview, (b) run pre-flight
(`build_preflight`), and (c) an explicit governed node *inside* a production
workflow.

- **`data/profile`** owns **tiers 1+2** universally and **escalates to 3a** when a
  workflow declares `strictness: strict` + `record_schema` (prep does; RCA/RAG
  don't). Cheap + hermetic → safe on the fast pre-flight path. Reuse in real runs:
  a first-node input-gate ("block if required roles missing, then proceed").
- **`data/expectations`** owns **tier 3b** — a governed **data-quality gate** for
  real pipelines (statistical/distributional/drift expectations), and on the
  pre-flight only when a suite is declared. It runs a **user-authored declarative
  suite** and emits a governance verdict (the same block/what-if/adopt idiom as
  the metric gates). Reuse in real runs: e.g. after data-prep, before training —
  "the expectations suite must pass or the promote is blocked." This is **not
  deferred**; it is simply kept *off* the fast Playground pre-flight so that path
  stays cheap.

**Engine choice for `data/expectations`: lean Pandera, not GX-the-library.** GX's
Data-Context/Checkpoint machinery is heavy and awkward to embed hermetically;
Pandera is schema/decorator-based, pandas+Pydantic-native, and fits the
optional-extra + deterministic-fallback + hermetic-test rule. Keep the door open
to *importing* GX suites, but don't make GX the engine. `data/expectations`
**generalizes the hardcoded `dataset_checks`** (dedup/leakage/PII/format — today a
fixed tier-3b): the curated checks become built-in expectations and users add
their own suite, rather than maintaining two parallel mechanisms.

## Decision — `data_expectation` (declaration) + `data/profile` (operator) + two wires

### A. `data_expectation` — a small declaration next to the input schema

Authored by the pack (it is **data**, per ADR-0026), a few lines; `unstructured:
true` for RAG/logs. It sits alongside — does not replace — the input JSON Schema.

```yaml
data_expectation:
  kind: rows                 # rows | spans | metrics | logs | documents
  container: [jsonl, csv, parquet, hf]      # accepted wire formats (loader picks)
  format_families: [openai_chat, alpaca, completion]   # optional, auto-detected
  roles:
    - {name: output,    required: true,  aliases: [output, completion, response]}
    - {name: reference, required: false, enables: "correctness scoring"}
  strictness: profile        # profile (tiers 1+2, default) | strict (adds tier 3a)
  record_schema: null        # Pydantic model / JSON-Schema ref — used only when strict
  binds_to: path             # which input arg receives the (materialized) source
```

`binds_to` is the missing wire: it names the input argument the profiled/loaded
source feeds — so an upload of inline rows can be **materialized to a path** and
bound to `path` when the workflow wants a path. `strictness`/`record_schema`
select the validation tier (next section).

### B. `data/profile` — one operator, `side_effect_free: true`

Because it's side-effect-free it runs **live** in the Playground sandbox (per the
ADR-0073 read/write split). Given `(source | sample, data_expectation)`:

1. **Normalize** — pick the loader by `kind`, reusing what exists:
   `dataset_checks/_io.load_rows` (rows), the `traces/analyze` reader (spans),
   `rag/readers.read_source` (documents); metrics via an OpenMetrics/Prom parser
   (optional extra, deterministic fallback). Source may be a path, HF id, URL, or
   an **uploaded sample materialized to a scratch-S3 path**.
2. **Map** — resolve each role to a real column: explicit mapping ▸ alias ▸
   auto-detect (`dataset/format._detect_format`).
3. **Profile** the sample — record count, detected format, per-role coverage %,
   type conformance.
4. **Verdict** (the shape the Playground verdict/what-if already renders):
   required role unresolved → **BLOCK** ("map column → role, or add field Y");
   optional missing → **DEGRADE** ("skipping dependency graph — no `parent_id`");
   else **PASS** ("1,000 records · format `openai_chat` · all roles resolved").

### C. Two wire points, both reusing existing rails

- **Playground upload/preview** (`POST /playground/upload` already samples): run
  `data/profile` on the sample against the selected workflow *before* the run →
  instant "fits / here's the gap," and materialize→`binds_to` fixes the 422.
- **Every run's pre-flight** (`build_preflight`, ADR-0072 / #797 / #800): the same
  profile becomes a governed pre-run gate for scheduled and prod runs too — not a
  Playground-only nicety.

### The situation matrix — every case is just `kind` + `roles`

| Situation | kind | required roles | degrades-on | strictness |
|---|---|---|---|---|
| Dataset **prep** (fine-tune) | rows | a format family (messages / prompt+completion / text) | — | strict |
| Dataset **eval** (judge) | rows | input, output | reference, context | strict on output |
| Incident RCA — **logs** | logs | message/text | timestamp, level, service | loose |
| **Metrics** | metrics | name, value, timestamp | labels | mapping |
| **Spans (OTel)** | spans | service, operation, duration | trace/span/parent id, status | mapping + degrade |
| **RAG ingestion** | documents | content | source, title, metadata | unstructured |

## What we reuse — explicitly NOT rebuilding

`coerce_and_validate` + `Draft202012Validator` (the envelope gate stays as the
first line); `dataset_checks/_io.load_rows` (multi-format loader); `traces/analyze`
field-mapping + reader; `rag/readers.read_source`; `dataset/format._detect_format`;
`build_preflight` (#797/#800) verdict machinery; `parse_and_sample` (#838); the
Playground verdict renderer; the `dataset/format`/`data-expectations` acceptance
seam (#745). New: the `data_expectation` declaration, the `data/profile` operator
+ verdict, thin per-`kind` adapters (mostly wrapping the loaders above), and the
upload→materialize→`binds_to` wire.

**One structural constraint:** the loaders (`load_rows`, the traces reader) live
*inside* operator packs, and operator packs must not cross-import each other
(pack rule). So the shared multi-format loading + `_detect_format` should be
**lifted to core** (e.g. `cairn/core/data/loaders.py`) and both the domain
operators and `data/profile` depend on core — not on each other. That lift is the
only non-trivial refactor this ADR implies.

## Consequences

- **Positive:** one seam validates *any* data situation against the selected
  workflow; honest "will my data run here" *before* running (Playground + prod);
  closes the upload 422; stays in cairn's "tell me what will happen" idiom
  (diagnostic-first, hard-block only on genuinely-required roles).
- **Cost / risk:** every data-consuming pack must author a `data_expectation`
  (small; a lint can flag missing ones and fall back to envelope-only). Auto-
  detection can mis-map exotic schemas → always allow an explicit override, and
  never silently drop a required role.
- **Non-goal:** this is not row-level content validation (PII/leakage/format) —
  that stays in the domain operators at run time. `data/profile` answers "does the
  shape fit," not "is the content good."

## Alternatives considered

1. **Per-workflow bespoke validators.** Rejected — O(workflows) maintenance; no
   shared "will it run" surface; exactly the sprawl the role model avoids.
2. **Demand a canonical wire format per kind (OTLP, OpenAI-chat, …) and reject
   the rest.** Rejected — hostile to "bring your own data"; `traces/analyze`
   already proves field-mapping + defaults absorbs real-world variance.
3. **Envelope JSON Schema only (status quo).** Rejected — validates arguments,
   not the payload behind a `path`; the failure surfaces mid-run, too late.
4. **Great Expectations / Pandera as the engine.** Deferred — heavy, dataframe-
   centric; fine as an optional deep-profiling extra later, wrong as the base
   seam (which must stay hermetic + span non-tabular kinds like spans/documents).

## Open questions (for discussion)

- Does `data_expectation` live in the **pack manifest**, the **operation YAML**,
  or derive from the **operator** that consumes the data (so composed workflows
  inherit it)? Leaning operator-declared + workflow-aggregated.
- `metrics` kind: adopt the OpenMetrics/Prometheus text exposition as the
  canonical container, or field-map a JSON/CSV metrics dump like spans? (Both, via
  the adapter.)
- Should a **BLOCK** verdict offer an inline **column→role mapping** UI in the
  Playground (n8n-style "pin & edit"), or only report the gap for v1?
- `data/expectations` (tier 3b): **Pandera** engine vs. GX-suite import vs. a small
  built-in expectation DSL? (Leaning Pandera as the engine + optional GX import.)
- Does `data/expectations` **generalize** `dataset_checks` (curated checks become
  built-in expectations, users extend) or run **alongside** it? (Leaning
  generalize — avoid two parallel tier-3b mechanisms.)
- Build order: **(1)** lift loaders + `_detect_format` to core → **(2)**
  `data/profile` `rows`+`spans` (covers eval/prep + RCA) + the two wires →
  **(3)** `data/profile` `documents`+`metrics` → **(4)** `data/expectations`
  (Pandera) as the governed data-quality gate for real pipelines.
