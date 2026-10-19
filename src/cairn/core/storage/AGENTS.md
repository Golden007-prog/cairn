# cairn/core/storage — runs, events, artifacts, pipeline records and the other stores

**Purpose.** Persist the governed record: `Run`/`RunEvent` behind `RunStore`
(memory/sqlite/postgres), `PipelineRecord`, content-addressed artifacts with lineage and
tags, training runs, judges, reviews, eval ledger, fingerprints, corpora (the capture store
left for `packages/cairn-platform`, #2421).
**Pattern (observed, not a rule anyone chose).** Each store is a hand-rolled triad
`InMemory* / Sqlite* (raw sqlite3 + to_thread + lock) / Postgres* (asyncpg, PgSchemaInit)`
selected by `CAIRN_STORAGE`. ~30 such triads exist across `storage/` and `cairn/core/*.py`.
ADR-0083 R4 proposes one substrate; it is not built — do not start it inside a feature PR.
**Does not own.** Enforcement, execution, route shapes.

**Contracts.** `RunStore` ABC (`protocols.py:426`); `Run` fields (JSON blob + indexed
columns) — adding a field is additive, never rename; `ArtifactStore` (`artifacts.py:75`):
registration is control-plane-only from `ArtifactDescriptor`s, versions immutable, tags the
only mutable thing; `PipelineRecord` shape is read by both pipeline owners and the console.
Schema changes: `migrations.py` helpers for sqlite, `pg_schema_init.py` for Postgres —
`CREATE TABLE IF NOT EXISTS` alone silently skips column changes (#255).

**Allowed deps.** `core/<top>` utilities, `dataflow` (descriptors), `services` (lazy: memory,
rag), `policy`/`oauth`/`operator` types. **Forbidden.** operator packs, `cairn.packs.*`,
`engine`/`dispatch` logic, **new** `triggers` imports (`backend.py:95-107` is debt).

**Testing.** `tests/core/test_storage_resolver.py`, `test_artifact_registry.py`,
`test_artifact_bytes.py`, `test_run_lineage.py`, `test_train_pins.py`,
`test_policy_pg_store.py`, `test_knowledge_corpora.py`, and every `*_store` test. Run with
`env -u CAIRN_STORAGE` locally.

**Common mistakes.** Adding a store with only sqlite; a new env knob instead of
`CAIRN_STORAGE`; blocking sqlite calls on the event loop; hydrating full `result_payloads` in
list endpoints; forgetting the workspace scope on a new table.

**Read.** `docs/architecture/09-storage-and-provenance.md`, ADR-0004, ADR-0047, ADR-0050,
ADR-0081 D2.
