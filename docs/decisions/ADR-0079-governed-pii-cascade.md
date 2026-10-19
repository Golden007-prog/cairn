# ADR-0079 — Governed tiered PII cascade: one Presidio pipeline, regex as a tier

Status: Proposed (2026-07-18)

Relates to / builds on: ADR-0074 (data-validation profile — PII density is a
readiness signal), ADR-0070 (config scoping — the per-entity policy is
governance-scoped config), ADR-0076 D3 (dataset-fix — `anonymize` is the fix
node), ADR-0078 S1/S2/S4 (atomic writes, quarantine sink, Shardable manifest —
the durability substrate this rides), #964 (batched Presidio), #379 (re2
regexset). Companion analysis: the PII-operator audit (2026-07-18).

## Context — the current Presidio operator is both wrong and slow

`operator-dataset-checks/_pii.py` ships three **mutually-exclusive** backends —
`resolve()` picks exactly one of `regex` / `regexset` / `presidio`. That is the
"two unrelated pipelines" anti-pattern, and it produces concrete defects:

1. **Correctness — the Presidio path loses secret detection.** The presidio
   backend builds a bare `AnalyzerEngine()` with **no custom recognizers**
   (`_pii.py:174`). cairn's own credential patterns (`AKIA…`, `sk-…`, `ghp_…`,
   JWT, private-key) live only in the `_REGEX` dict and are invisible to
   Presidio. Choosing "presidio" for higher recall silently **drops the
   highest-risk entities** (keys/tokens) that regex caught.
2. **Correctness — the `kinds` filter is vocabulary-mismatched.** `analyze(
   entities=kinds)` is passed cairn's regex vocabulary (`email`, `ipv4`,
   `aws_access_key`), but Presidio emits `EMAIL_ADDRESS`, `IP_ADDRESS`,
   `PERSON`. A governance config that sets `kinds` to the regex vocabulary +
   `backend=presidio` filters to entity types Presidio never emits → **redacts
   nothing**. A silent governance hole.
3. **Speed — spaCy NER runs on every string leaf indiscriminately.**
   `iter_strings` sends *all* strings through `analyze()`; there is no cheap
   deterministic pre-filter and no "only NER the free-text fields." NER is
   ~10–50× slower than regex, so a 200k run pays NER on strings a regex would
   resolve in nanoseconds.
4. **Speed/RAM — the model reloads per shard worker.** `@lru_cache` is
   per-process, so each Shardable child loads `en_core_web_lg` (~500 MB)
   separately → ~4 GB of models across 8 workers on an 8 GB box → OOM risk.
5. **No content-hash cache** — identical/templated strings (ubiquitous in
   training data) are re-analyzed every time.
6. **Redact-only anonymization** — Presidio's `AnonymizerEngine` operators
   (hash/encrypt/mask/custom) and per-entity policy are unused.

## Decision

Replace "pick one backend" with a **single governed cascade**, built almost
entirely from Presidio's own machinery (verify-upstream: `RecognizerRegistry`,
`PatternRecognizer`, `AnalyzerEngine`, `AnonymizerEngine`/`OperatorConfig`
already provide the cascade, merge, context, and per-entity actions). The regex
tier is **absorbed, not retired**.

### The cascade (new `backend="cascade"`)

```
string leaf → RE2 deterministic pre-filter (secrets/keys/email/IP, checksums)
            → [free-text?] → Presidio spaCy NER (person/location/org/…)
            → AnalyzerEngine merges overlapping detections (Presidio's job)
            → per-entity OperatorConfig: redact | pseudonymize | hash | encrypt | generalize
            → high-risk (credential) hit → quarantine row (ADR-0078 S2 sink)
```

- **Deterministic tier** = cairn's `_REGEX` patterns registered as Presidio
  `PatternRecognizer`s (entity types `AWS_ACCESS_KEY`, `OPENAI_KEY`, …) + the
  built-in `CreditCardRecognizer` (Luhn — closes the credit-card gap) inside one
  `RecognizerRegistry`. Fast, deterministic, and now **inside** Presidio.
- **Contextual tier** = the spaCy NER recognizers, run only where they add value.
- **Merge** = `AnalyzerEngine` does it; we do not write a merger.
- **Policy** = per-entity `OperatorConfig`, expressed in the governance
  `config_schema` (ADR-0070): credentials→remove/quarantine, IDs→pseudonymize
  (stable `Alice→PERSON_0042` via one custom Operator), age/address→generalize.
- **Metadata** = `RecognizerResult` carries `entity_type/start/end/score/
  recognizer_name`; emit a detection sidecar (offsets + action + policy_version,
  never the raw value).

### Speed levers (all reuse existing substrate)

- **RE2 pre-filter**: the deterministic tier resolves the bulk of rows without
  touching spaCy — the single biggest win.
- **Content-hash cache**: scan each unique string once, reuse by hash.
- **Model reuse + worker cap**: one pipeline per worker, `CAIRN_BATCH_MAX_WORKERS`
  capped; `en_core_web_sm` offered as a faster NER tier alongside lg.
- **Selective recognizers**: enable only the entity types the dataset needs.
- Keep batched `nlp.pipe` (#964) + the Shardable pool + S4 manifest.

## Regex operator — absorbed, not retired

The `regex`/`regexset` backends **stay**:
- their patterns become the cascade's deterministic tier (this is how secrets
  get back into the Presidio path);
- **standalone regex-only** remains the right tool for low-risk operational logs,
  latency-sensitive online paths, and — critically — the **hermetic base
  install** with no Presidio/spaCy (the zero-dep fallback keeps tests and
  un-extra'd deployments working).

What retires is the *model* that regex and Presidio are rival, mutually-exclusive
backends. Regex becomes a tier and a fallback, not a competitor.

## What is BUILT vs WIRED

**Build (pack-local):** the `cascade` backend (registry assembly + RE2
pre-filter routing + content-hash cache), the per-entity `OperatorConfig` policy
map + stable-pseudonymize Operator, the detection-metadata sidecar + high-risk
quarantine, a PII eval op (precision/recall/F1 per entity against gold spans).

**Wire (exists):** Presidio registry/analyzer/anonymizer/operators, re2, the
Shardable pool + S4 manifest, the S2 quarantine sink, the governance
`config_schema`, the `check→fix→check` loop, `eval/judge` for QA sampling.

## Non-goals

- Not a new detection/merge/context engine — that is Presidio's `AnalyzerEngine`.
- Not a cloud DLP dependency — local RE2→Presidio cascade only.
- Not removing the hermetic regex fallback.
- Not changing default behavior until a caller opts into `backend="cascade"`.

## Consequences

**Positive:** the Presidio path stops losing credentials; one consistent
vocabulary + merge; per-entity governance (pseudonymize/generalize/quarantine);
a real precision/recall eval; and a 200k run dominated by the fast deterministic
tier instead of blanket NER.

**Negative/risks:** more Presidio surface to configure; pseudonymization state
(the `Alice→PERSON_0042` map) is sensitive and must be access-controlled + versioned;
`en_core_web_sm` trades some recall for speed — the tier choice is a governance
decision, not a silent default.

## Phasing

1. **Cascade backend** (registry + RE2 pre-filter + cache + fix the `kinds`
   vocab + model-reuse) — the correctness + speed core. *(this issue)*
2. Per-entity `OperatorConfig` policy + stable pseudonymize.
3. Detection-metadata sidecar + high-risk quarantine.
4. PII eval op (gold spans from `ai4privacy/pii-masking-200k`).

## Status / next step

Proposed — design done. Phase 1 in progress.
