# Dataset preparation and readiness

## Purpose
Get data into a hash-pinned, admitted dataset version before it costs GPU time: import or
pull, normalise, dedup, decontaminate against eval sets, redact PII, register, and record a
readiness verdict a launch door can read. Trace: [../golden-paths/dataset.md](../golden-paths/dataset.md).

## Owns (OBSERVED)
- Domain pack `cairn/packs/dataset_readiness/` — pipelines `import` (one `flow: clean` leg), `readiness` (agentic `template: check` + bounded `loop: remediate` over `flow: clean`, `on_exhausted: gate`, nightly `on: cron`), `pull-and-admit` (`task: ingest/pull`), `api-and-admit` (`task: ingest/api`), `watch-source` (`task: ingest/watch` → pull → clean → commit → `monitor/propose` → `monitor/dispatch`); flows `clean.yaml` (data), `check.md`, `prep.md`, `decontaminate.md`, `promote.md`, `watch.md` (agentic); `skills/`, `policies/`, `evals/`, `examples/`, `rag/governance` corpus (role `data_governance`), `[[demo.check]]` tiers.
- Operator pack `cairn/operators/official/dataset_checks/` — 21 operators (`dataset/profile`, `dedup`, `dedup_apply`, `decontam`, `decontam_apply`, `pii_scan`, `anonymize`, `format`, `split`, `sample`, `gate`, `prep_gate`, `prep_review`, `curate`, `provenance`, `publish_fingerprints`, `replay`, `scan_all`, `capture_to_{sft,pairs,rewards}`) + dataflow stages (`ingest.normalize`, `dedup.exact`, `pii.redact`, `export.jsonl`, `sample.rows`…) registered under the stage entry-point groups; extras `[pii]` (Presidio), `[fastpii]`, `[minhash]`, `[fast]`; `DESIGN.md` present; 47 tests.
- Supporting packs: `data_profile` (builtin), `data_expectations` (Pandera), `ingest` (`ingest/pull`, `ingest/api`, `ingest/watch`; SQLAlchemy + httpx), `dataset_synth`.
- Core: `storage/artifacts.py` (versions, roles, lineage, tags), `dataset_roles.py`, `dataset_readers.py`, `intake.py` (intake verdict door), `holdout.py` (holdout door), `storage/fingerprints.py`, `eval_cutoff.py`, `services/data/` (loaders, profile, quality), `dataflow/manifest.py`.

## Does not own
Training, the engines that execute `clean.yaml` (cairn-operator-dataflow), the console Datasets view (reads `routers/datasets.py`).

## Public interface
`dataset:<name>[@N]` references (`storage/artifacts.py:1186 parse_dataset_ref`) accepted
anywhere a pipeline input has `type: Dataset`; the `Dataset` artifact type in pipeline
`produces/consumes`; `POST /datasets/*` (import staging, preview, register); the
readiness/import pipelines.

## Dependencies
Operator pack → `cairn.core.operator` (31), `cairn.core.storage` (4), `cairn.core.llm` (1), `cairn.core.dataflow` (1), `cairn.core.<top>` (2). Domain pack → `cairn.core.pack`.

## Side effects
Reads staged uploads / S3 / SQL sources / HTTP APIs; writes JSONL exports to the artifact store; registers versions; Presidio/spaCy NER when the `pii` backend is presidio; LLM judge calls in `curate`/synth.

## State
Artifact versions + roles + intake verdicts (`storage/artifacts.py`), fingerprints (`storage/fingerprints.py`), watch markers (`StoredSchedule` rows via `ingest/watch`).

## Entry points
`POST /start {ref: "dataset-readiness/import"}`; console Datasets → Add dataset (calls `/start`); `cairn pipeline run dataset-readiness/readiness`; the `watch-source` pipeline fired by a declared `watch` trigger; `flow: clean` inside other packs' pipelines is **not** allowed (ADR-0084 D5) — training has its own `readiness.yaml`/`facts-prep.yaml` copies.

## Tests
`tests/core/test_dataset_readiness_pack.py`, `test_dataflow_clean_template.py`, `test_datasets_api.py`, `test_watch_source.py`, `test_artifact_registry.py`, `test_finetune_dataset_preflight.py`, `tests/core/test_dataset_*` (6), `test_leg_lineage_resolution.py`; pack-local `dataset_checks/tests` (49), `ingest/tests` (**not in `testpaths`**), `data_profile/tests`, `data_expectations/tests`.

## Invariants
| Invariant | Status |
|---|---|
| A training run's dataset refs are checked against holdouts and intake verdicts at admission | CONFIRMED on the dispatcher path (`dispatcher.py` holdout/intake doors); flow legs stamp verdicts (`leg_executor.py:430`); CLI local path bypasses (see [08](08-governance.md)) |
| Data crosses legs only as persisted artifacts (manifests + scalars) | CONFIRMED (`dataflow/contract.py:26 _assert_scalar_payload`; ADR-0081 D3) |
| The watch pipeline admits only through the governed import | CONFIRMED (`tests/core/test_watch_source.py::test_the_watch_pipeline_admits_through_the_governed_import_only`) |
| Golden datasets are guarded from overwrite | OBSERVED helper `_guard_golden` (`artifacts.py:452`); test coverage UNCLEAR |
