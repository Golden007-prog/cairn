# cairn/core/pack — the Pack contract, manifest, discovery, registry, profiles

**Purpose.** The domain-neutral seam between engine and packs: `Pack` (`protocol.py`),
`PackManifest`/`load_manifest` (`manifest.py` — the `pack.toml` grammar), entry-point +
`CAIRN_PACK_PATH` discovery (`loader.py`), the process-global registry and its `all_*()`
aggregators (`registry.py`), composition validation (`composition.py`), `Profile`
(`profile.py`), curation/visibility/drift/provisioning.
**Owns.** How packs are found and what they may declare. **Does not own.** Pack content
semantics (engine, dataflow, policy read the dirs), installation state
(`workflow_registry.py`, `registry_install.py`), operator discovery (`operator/loader.py`).

**Contracts.** The `pack.toml` tables are LOCKED by ADR-0020 (amended in practice: `kind`
replaces `type`; a domain pack has exactly one `__init__.py` calling `Pack.from_manifest`).
Unknown tables/keys fail loudly (`_check_unknown_tables`). The engine reads pack content
**only** through `registry.all_*()`; an installed pack always wins a name collision with a
dev-dir pack. Adding a manifest field = `manifest.py` + `Pack` + the consumer + `cairn.lock`
regeneration + ADR note if the table is locked.

**Allowed deps.** `core/<top>` (settings types), `operator` (entry types), `schema`.
**Forbidden.** operator packs, `cairn.packs.*`, `engine`/`dispatch`, **new** `triggers`
imports (`registry.py:50`, `composition.py:324/341` are debt).

**Testing.** `tests/core/test_pack_manifest.py`, `test_pack_manifest_composition.py`,
`test_pack_from_manifest.py`, `test_pack_manifest_drift.py`, `test_installed_pack_manifests.py`,
`test_init_pack_is_data_only.py`, `test_pack_*` (16), `tests/packs/`.

**Common mistakes.** Importing a pack module from core to find its files
(`models_catalog.py`, `recommender.py` do this — do not copy); a new aggregator that scans
the filesystem instead of the registry; treating `CAIRN_PACK_PATH` packs as installed.

**Read.** `docs/architecture/06-packs.md`, ADR-0020, ADR-0003, ADR-0026, ADR-0092,
`cairn/packs/CLAUDE.md`.
