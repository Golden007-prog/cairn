# cairn-operator-data-profile — `data/profile`

Semantic data profiler (ADR-0074). Given records (an inline sample or a
`source`) and a workflow's `DataExpectation` — the semantic **roles** its
operators read, not a wire format — it answers *will this data run here?* and
emits a **PASS / DEGRADE / BLOCK** verdict with per-role coverage.

- **Tier 1** (format/parse) — handled by the shared loader (`cairn.services.data`).
- **Tier 2** (semantic roles) — resolve each role to a real column (exact ▸ alias);
  a missing **required** role blocks, a missing **feature-gating** role degrades.
- **Tier 3a** (per-record structural, `strictness: strict`) — validate every record
  against `record_schema` (JSON Schema) or the built-in chat/alpaca/completion checks.

`side_effect_free`, zero heavy deps → ships with the base install (builtin) so the
Playground preview and the run pre-flight can always call it, and it composes as a
cheap input-gate node in real pipelines. Statistical/dataset-level quality is the
separate `data/expectations` operator (tier 3b).
