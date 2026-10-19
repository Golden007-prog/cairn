# ADR-0128 — A third repository, `cairndev-sh/cairn-extensions`, for public operators, contracts and packs the example does not prove

- **Status:** ACCEPTED 2026-10-15 by the owner, with the recommendation of each of the nine
  decisions at the end taken as written. Decision 7's service rows are resolved by ADR-0130
  (provider wheels).
- **Date:** 2026-10-15
- **Issue:** #2535 (owner decision of 2026-10-13: public extensions live in their own
  repository). **Amends** ADR-0121 D1/D2 (two repositories → three; the layout of the third)
  and ADR-0121 D3 (the names apply to the third repository unchanged). Restates ADR-0127 D5's
  placement rule, with its errata of 2026-10-15, as the general rule. Keeps ADR-0120 (what is
  public), ADR-0123, ADR-0124 and ADR-0127 D1–D4/D6–D8 unchanged. Sequenced after #2583 (the
  engine substrate leaves the builtin dataflow pack) for the first move it names.

## Context

ADR-0121 D1 decided two repositories: the public core `cairndev-sh/cairn` (fresh history) and
the private `cairn-platform` (history kept). ADR-0127 D5 then decided that the public core
ships only the connectors the four hello-cairn pipelines prove, and sent the `prefect`,
`metaflow` and `skypilot` engine adapters to a repository the layout ADR does not know:
`cairndev-sh/cairn-extensions`, created 2026-10-13 with an Apache-2.0 LICENSE and a README
and no code (`gh repo view`, 2026-10-15). The README already states the shape: "each
extension ships as its own wheel (`cairn-operator-*`, `cairn-contracts-*`, `cairn-pack-*`)
and declares the cairn engine versions it supports … nothing here is imported by the engine
itself".

Verified on `origin/main` 83419586 and the WP6 branch d677b221 (`examples/hello-cairn` with
its four pipelines, the flows they name and the two profiles):

- The public set is thirteen builtin operator packs (`BUILTINS`,
  `scripts/oss/assemble_public_tree.py:40`) and twelve contracts (`CONTRACTS`, `:45`); the
  `train-local` pack and `train.finetune` contract of ADR-0127 D4 are on neither branch yet.
- The builtin dataflow pack registers four `cairn.dataflow_engines` (`builtin`, `prefect`,
  `metaflow`, `skypilot`) behind three optional extras, and two stages (`noop`, `noop.slow`).
  The three adapters import the pack's runner (`operator_dataflow.engine.runner`), its
  `BuiltinLocalEngine`, its stage loader and its SSH fetch helpers; `engines_skypilot.py`
  also imports `engines_metaflow`. An operator pack importing another's module is forbidden
  with an empty allowlist (`tests/core/test_architecture_boundaries.py`, "operator pack →
  another operator pack"; only builtin → builtin is exempt). That is why ADR-0127's errata
  deferred the move to #2583.
- Core names the three engines only as data and through the registry: the SkyPilot door
  asks `load_engine_factory("skypilot")` and never imports (`cairn/core/skypilot_door.py:21`),
  `cairn doctor` carries install hints per engine name (`cairn/cli/commands/doctor.py:212`),
  and the flow export generates Prefect and Metaflow source text (`cairn/core/dataflow/export.py:54`,
  `EXPORT_TARGETS`). Two install hints name the builtin pack's extras
  (`skypilot_door.py:38`, `doctor.py:212-213`), as do the root `engines` extra
  (`pyproject.toml:306`), `Dockerfile.engine:76,249`, the `campaign` profile
  (`cairn/profiles/campaign.toml:37`, `dataflow_engine = "metaflow"`) and the chart's
  SkyPilot guard (`deploy/helm/cairn/templates/_helpers.tpl:190`).
- The monorepo produces the two trees from one rule set: the assembler names what is public
  (`assemble_public_tree.py`) and the carve reads those rules and classifies every tracked
  path as `public`, `platform`, `both`, `public-gap` or `dropped`; a path neither tree
  claims is `forgotten` and fails `tests/scripts/test_carve_platform.py`
  (`carve_platform.py:312-336`). The assembler's leak scan has import and text categories
  (`IMPORT_LEAKS`, `TEXT_LEAKS`, `:144-162`) and `scripts/check_public_set.py` refuses a CI
  venv holding any `cairn-operator-*`, `cairn-contracts-*` or `cairn-pack-*` the root wheel
  does not depend on.
- `[compat] cairn_engine` is a PEP 440 range checked once per pack against the `cairndev`
  distribution; a mismatch warns and loads (`cairn/core/pack/loader.py:374-423`). Every
  shipped pack declares `>=0.1.0`.

## Decision

**D1 — what the repository is, and the placement rule.** `cairndev-sh/cairn-extensions` is
the third repository of ADR-0121 D1: public, Apache-2.0, fresh history (as the core's), and
the home of first-party extensions that are public but that the public example does not
prove. The rule ADR-0127 D5 stated for its own scope is the general one:

> A public operator pack, contract or domain pack that the hello-cairn pipelines (ADR-0127
> D1) exercise end to end is the core's. One that is public but not proven by the example is
> an extension. One that is not public is the platform's (ADR-0120 D3).

The unit of placement is the wheel, never the operation: an operation the example does not
call stays with the pack that ships a proven sibling (D2 lists them). A contract lives with
whoever owns its meaning (#2535): a contract whose provider ships in the core stays in the
core (ADR-0120 D1); a domain contract whose only public implementer is an extension moves
with it; a contract with no public implementer stays private. The platform may hold the
private implementer of a public contract. The engine, the builtins and the core contracts
stay in `cairndev-sh/cairn`; the extensions repository holds no engine code, no console and
no process material (ADR-0121 D4, ADR-0124 D3).

Layout (mirrors ADR-0121 D2's `packages/` and the platform's `packs/`):

```
packages/cairn-operator-<name>/     operator packs, one wheel each (src/operator_<name>, pack.toml)
packages/cairn-contracts-<name>/    the domain contracts the operator packs implement
packs/cairn-pack-<name>/            public domain packs (data and glue, ADR-0127 D2)
tests/                              the packs' tests and the conformance runs (D3)
docs/                               a README per package; no architecture docs (they are the core's)
```

**D2 — the inventory that moves first, and what stays.** The table is the public set on
`origin/main` 83419586 read against the four pipelines, the four flows and the two profiles
on the WP6 branch (d677b221), plus the `train-local` pack ADR-0127 D4 adds. "Proven by"
names the pipeline, flow or profile that calls the item; "bound" means the hello profile
binds it and nothing calls it.

| Item | Kind | Proven by | Placement | Gate / reason |
|---|---|---|---|---|
| `dataflow_engines: prefect` (`engines_prefect.py`, `prefect_bridge.py`, extra `[prefect]`) | engine adapter, builtin dataflow pack | nothing | **moves** | after #2583 promotes the runner, result readers, stage loader and SSH fetch helpers into `cairn.core.dataflow`; then the adapter imports only the contract roots |
| `dataflow_engines: metaflow` (`engines_metaflow.py`, `metaflow_bridge.py`, `metaflow_train_bridge.py`, extra `[metaflow]`) | engine adapter, builtin dataflow pack | nothing | **moves** | same gate; also the `campaign` profile, `doctor.py:212`, the root `engines` extra and `Dockerfile.engine` retarget (D6) |
| `dataflow_engines: skypilot` (`engines_skypilot.py`, extra `[skypilot]`) | engine adapter, builtin dataflow pack | nothing | **moves** | same gate; it imports `engines_metaflow` and `engines_ssh`, so it ships in the same wheel as metaflow or #2583 promotes what it borrows (owner decision 2); `skypilot_door.py:38` and the chart guard retarget (D6) |
| `dataflow_engines: builtin`, stages `noop`, `noop.slow`, `cairn-dataflow-run` | engine, stages, task-image entry | `engine-check` and `summarise` flows (profile-and-publish, nightly-ops) | stays | proven |
| `dataflow/run`, `ml/run`, `artifact/promote` | operations, builtin dataflow pack | every flow leg; `lora` (kind `ml`); profile-and-publish, tiny-finetune | stays | proven |
| `data/profile` | operation, `data_profile` | profile-and-publish, tiny-finetune | stays | proven |
| `redact/text` | operation, `redact_regex` | profile-and-publish | stays | proven |
| `storage/put` | operation, `artifact_local` | profile-and-publish, tiny-finetune | stays | proven |
| `storage/get` | operation, `artifact_local` | bound (`hello.toml`), not called | stays | same wheel as `storage/put` (D1: the wheel is the unit) |
| `audit/store` | operation, `audit_store` | profile-and-publish, nightly-ops | stays | proven |
| `mcp/call` | operation, `mcp` | `assistant.md` (ask-the-docs) | stays | proven |
| `rag/search`, `rag/rerank` | operations, `rag_local`, `rag_rerank` | `assistant.md` | stays | proven |
| `rag/fetch` | operation, `rag_local` | bound, not called | stays | same wheel as `rag/search` |
| `memory/write` | operation, `memory_local` | `assistant.md` | stays | proven |
| `memory/search` | operation, `memory_local` | bound, not called | stays | same wheel as `memory/write` |
| `monitor/threshold`, `monitor/propose` | operations, `monitor_threshold` | ask-the-docs, tiny-finetune; nightly-ops | stays | proven |
| `monitor/dispatch` | operation, `monitor_threshold` | nothing | stays | same wheel; its launch handle is core's (ADR-0120 D7, #2455) |
| `remote/exec` | operation, `remote` | `nightly-job.md` (nightly-ops) | stays | proven |
| `secrets/env` | operation, `secrets_env` | `hello.toml` binds `secrets.store`; `nightly-job.md` calls the capability | stays | proven |
| `secrets/infisical` | operation, `secrets_infisical` | `hello-infisical.toml` | stays | ADR-0127 D3; proven only when the `infisical` compose profile is in the matrix (owner decision 6) |
| `train.lora` stage, `train/eval-loss` | `train-local` pack (ADR-0127 D4; not yet on either branch) | tiny-finetune | stays | ADR-0127 D4 |
| the 12 public contracts (`dataflow`, `audit`, `mcp`, `monitor`, `artifact`, `storage`, `memory`, `redact`, `secrets`, `rag`, `remote`, `data`) and `train.finetune` | contracts | their builtin providers | stays | ADR-0120 D1: a contract whose provider ships in the core ships in the core |
| `eval`, `dataset`, `dataset-stages`, `ingest` | contracts, private | — | platform | ADR-0120 D3; each moves here only with a public implementer |
| MCP presets `github`, `slack`, `notion` (`cairn/connectors/`) | named server specs | the core Slack notifier, `cairn connect --preset` | stays | ADR-0127 errata |
| in-process and DBOS runtimes; SQLite and Postgres stores and buses; sqlite-vec and pgvector | core services | the ADR-0127 D1 matrix | stays | proven |
| Temporal runtime (`runtime-temporal`); NATS job channel (`nats`) | core services | nothing | stays | ADR-0127 D6: installable, documented; not a pack, so not placeable here |
| Qdrant driver (`cairn/core/storage/rag.py`, `rag-qdrant`); Pinecone stub (`rag-pinecone`) | core service, extras | nothing | **undecided** | a core service, not a pack: there is no service-provider entry-point group to move it through, and adding one is a CLAUDE.md §5 decision (owner decision 7) |
| Presidio cascade (`pii`: `cairn/core/dataflow/conformance.py`, `engine/python_function_registry.py`, `operator/batch.py`, `cairn doctor`) | core service, extra | nothing | **undecided** | same reason; #2535's rationale ("Presidio stays out of the core") is not what the code does today (owner decision 7) |
| `rag` extra (llama-index, trafilatura, fastembed; `cairn/services/rag/{readers,ingest,query,lifecycle}`) | core service, extra | ask-the-docs indexes `rag/docs` at boot through `services/rag/lifecycle.py`; `cairndev[hello]` does not pull `[rag]` | **undecided** | whether the proof needs the extra is WP6's matrix run to show; a core service either way |
| `optuna` extra (root `pyproject.toml`) | extra | nothing; no consumer in `cairn/core`, `cairn/services` or `cairn/cli` (its consumers are the private training pack and the platform) | **undecided** | leaves the public extras with the platform rather than moving here (owner decision 8) |
| Modal and Kaggle job providers | `cairn-operator-training` (platform) | — | platform | ADR-0127 D5; core holds only their connection `ProviderSpec` rows |

Nothing else in the public set is unproven. The official tier is the platform's (ADR-0120
D3); an official pack goes public through this repository under the move policy #2535
names (one at a time, fresh history, its contract with it), not by a rule in this record.

**D3 — names, compatibility, CI and cadence.**
- *Names.* ADR-0121 D3 applies unchanged: `cairn-operator-<name>`, `cairn-contracts-<name>`,
  `cairn-pack-<name>`, with the import names `operator_<name>` / `cairn_pack_<name>` and
  `publisher = "cairn"`. No `cairn-ext-` prefix: a distribution name records what a thing
  is, not where it is kept, and recorded identity (`pack/op` refs, contract IDs) must survive
  a move between repositories exactly as it survived the renames (ADR-0121 Consequences).
  The repository README already uses these names. Every name is reserved on PyPI before the
  first upload.
- *Compatibility.* Each extension's `pack.toml` carries `[compat] cairn_engine` as a bounded
  range, `>=X.Y,<X.(Y+1)` pre-1.0 (`">=0.1,<0.2"`), and its `pyproject.toml` pins
  `cairndev` to the same range, plus the public contract wheels it implements and the
  `cairndev[...]` extras it needs. The loader's warning stays a warning (`loader.py:374`); the
  bounded range is what makes it say something. A new engine minor is adopted by widening the
  range in a release of the extension, never by the engine.
- *CI.* The extensions repository runs on CircleCI like the core (ADR-0121 D7): `policy`,
  `python` (3.12–3.14) and `package` jobs, no self-hosted runner, DCO enforced. The `python`
  job installs the released `cairndev` from PyPI at the newest version inside each pack's
  `[compat]` range (never a checkout of the core), runs each pack's own tests, the public
  dataflow conformance suite against every engine adapter the tree ships
  (`tests/dataflow_conformance`, carried by the public tree — `TEST_DIRS`,
  `assemble_public_tree.py:63`), the manifest-drift and contract-conformance checks the core
  exports for installed packs (`cairn pack check`), and the inverse of
  `check_public_set.py`: the venv holds `cairndev`, its extras, the public contracts and the
  extension under test, and nothing named `cairn-platform` or `cairn-pack-*` private. A
  nightly job repeats the `python` job against the public `main` of `cairndev-sh/cairn`,
  as the platform's does (ADR-0121 D1), so a core change that breaks an extension is seen
  the next morning and not at the next release.
- *Cadence.* Each wheel releases on its own tag, `<distribution>/vX.Y.Z`, through PyPI
  trusted publishing on Actions (ADR-0121 D7); there is no repository-wide version. A core
  minor that an extension needs is followed by an extension release that widens
  `[compat]`; a core minor that an extension does not need changes nothing.

**D4 — how the monorepo produces it (design; no code in this record).** The monorepo stays
the source of truth for first-party extensions until the owner's move policy says
otherwise, and a third assembler target produces the extensions tree the way
`assemble_public_tree.py` produces the public one:
- *Source home.* A third operator tier, `cairn/operators/extensions/<name>/`, beside
  `builtins/` and `official/` (ADR-0027/0030: a tier move is a directory move), with
  `trust_level` and the packaging contract of the other two; extension contracts stay under
  `contracts/<name>/` and extension domain packs under `cairn/packs/<name>/`, each named in
  an `EXTENSIONS` table in the assembler (`EXTENSION_OPERATORS`, `EXTENSION_CONTRACTS`,
  `EXTENSION_PACKS`), the way `BUILTINS` and `CONTRACTS` name the public set today. A path
  not named there does not go to the extensions tree.
- *Assembler.* `scripts/oss/assemble_extensions_tree.py --out DIR`, reading the public
  assembler's module as the carve does (`carve_platform.py:38-40`), copies the three tables
  into the D1 layout, generates the meta files, CI and a root `pyproject.toml` whose
  workspace members are the extension wheels only, rewrites paths with the same helpers,
  and writes `ASSEMBLY_REPORT.md` with its own scan. `tests/_layout.py`'s `LAYOUT` block
  gains the `extensions` tier so a tree-walking test needs no literal.
- *Carve.* The carve's classifier gains the tree `extensions`: a path the extensions
  assembler claims is the extensions tree's, dropped from the platform as public paths are
  (`carve_platform.py:312-336`); a path two trees claim fails the test as today. The
  `engines` extra leaves `DEPENDENCY_EXTRAS` (`:181`) for the platform's own dependency on
  the extension wheels (D6).
- *Leak scans, both directions.* The public assembler's `IMPORT_LEAKS` gains an
  "extensions operator import" category listing the extension packs' import names, so an
  engine or builtin module that imports one fails the public assembly; its `TEXT_LEAKS`
  learns the extension distribution names in config. The extensions assembler's scan
  forbids `cairn_platform`, `cairn.packs`, every official pack's import name, every
  *builtin* pack's import name (an extension reaches a builtin only through the registry,
  D5), the console backends and the private text patterns the public scan already has.
  `scripts/check_public_set.py` is unchanged: it already refuses any `cairn-operator-*`
  the root wheel does not depend on, which is what keeps the public CI proving the core
  installs alone (#2535's rationale).

**D5 — dependency direction, and the tests that hold it.** Extensions depend on the
`cairndev` wheel, its public extras and the public contract wheels; the platform may depend
on extension wheels; the core never depends on an extension, by import or by distribution.
In imports: an extension pack imports `cairn.core.operator`, `cairn.core.dataflow` for engine
and stage providers, `cairn.core.llm` / `cairn.core.eval` for those operator kinds (ADR-0120
D7, ADR-0123 D3), and its contract distributions — never another operator pack's module,
builtin or official, never `cairn_platform`, never dispatch, engine or triggers internals. An
extension that needs a builtin's *operation* at run time names the builtin wheel as an
install dependency and reaches the operation through the registry or `OperationContext`
(owner decision 3). What enforces it:
- `tests/core/test_architecture_boundaries.py` — the existing rules already cover the new
  tier, because every predicate keys on `cairn/operators/`: "operator pack → another
  operator pack" forbids extension → builtin and extension → official with its empty
  allowlist (the builtin → builtin exemption does not extend to `extensions/`); `R_PACK_CORE`
  bounds what an extension imports from `cairn.*`; `R_OPERATOR` forbids core, CLI and
  services importing any `operator_*`. The file gains one rejecting case per new edge
  (extension → builtin, extension → platform) in `test_boundary_rule_can_fail`'s table, and
  `R_PLATFORM`'s predicate adds `cairn/operators/extensions/` to its roots (the public core
  never imports the platform; an extension never does either).
- `tests/core/test_public_core_boundary.py` gains the distribution-level rule: no base
  dependency and no public extra of the root `pyproject.toml` names an extension wheel, with
  a rejecting case (an `engines` extra that names one).
- `tests/core/test_distribution_names.py` already requires every operator pack's
  distribution to be `cairn-operator-<dir>` and its publisher `cairn`; the extensions tier
  joins the globs it walks through `tests/_layout.py`.
- `tests/scripts/test_assemble_public_tree.py` gains the extensions-import leak with its
  rejecting case; a new `tests/scripts/test_assemble_extensions_tree.py` proves the D1 layout,
  the three tables, the scan and the generated CI, as the public one does.

**D6 — what the platform does.** The platform depends on the extension wheels it uses,
pinned to a SemVer minor like `cairndev` (ADR-0121 D1): `cairn-platform`'s dependencies gain
`cairn-operator-<engines>[metaflow,skypilot]` (the shape the engines wheel takes is owner
decision 2) where today the root `engines` extra pulls
`cairn-operator-dataflow[engine,prefect,metaflow]` (`pyproject.toml:306`), and the carve's
`platform_pyproject` writes that dependency instead of carrying the extra. The public root's
`engines` extra is dropped by the assembler as now (`PRIVATE_EXTRAS`, `:102`); the public
dataflow pack keeps `[engine]` and `[ssh]` and loses `[prefect]`, `[metaflow]`, `[skypilot]`.
The install hints retarget to the extension's distribution and extra (`skypilot_door.py:38`,
`doctor.py:212-213`), `Dockerfile.engine:76,249` installs the extension wheel on the
platform image only (the public engine image installs none), the chart guard at
`_helpers.tpl:190` keeps its message and names the new distribution, and the `campaign`
profile (`cairn/profiles/campaign.toml:37`), which selects `metaflow`, either moves to the
platform or is carried public with a `cairn doctor` row that names the missing extension
(owner decision 5). The flow export's generated Prefect and Metaflow source
(`cairn/core/dataflow/export.py:54`) emits text and imports nothing, so it is not a boundary
edge; whether it stays in core or is supplied by the adapters through registry metadata, as
ADR-0120 D5 reads, is owner decision 4. Pipeline records are untouched: engine names in
`profile.backends` and `EngineJobHandle.engine` are entry-point names, not distributions.

## Consequences

- Three repositories, one rule set: the public assembler names the core, the extensions
  assembler names the extensions, the carve keeps the rest and refuses a forgotten path.
  The extensions tree is assembled and reviewed locally like the public one (#2428's
  rehearsal) before anything is pushed; its first push is one import commit.
- The public dataflow pack shrinks to the builtin engine, two stages, three operations and
  the task-image entry; `cairndev` loses three optional extras and the public engine image
  loses the SkyPilot `ssh`/`rsync` layer's reason to exist (`Dockerfile.engine:290-300`; it
  may stay for `remote/exec`).
- `docs/support-matrix.md` rows for Metaflow, SkyPilot and the Prefect bridge (`:33-35`)
  move to the extension's README and say which `cairndev` range the row was proven on;
  `docs/contracts/README.md`'s Integration row names the extensions repository as the
  adapters' home; `docs/architecture/06-packs.md` and `cairn/operators/CLAUDE.md` gain the
  third tier.
- What this does not change: the `Operation`, `pack.toml`, `DataflowSpec` and `Run`
  contracts; the entry-point group names; the public HTTP API; ADR-0127's gate for the flip
  (#2430 does not wait for the first extension release — the public core is the same with or
  without one installed).
- Behaviour without an extension installed: selecting `metaflow`, `prefect` or `skypilot`
  as a flow engine is refused at the launch door with the extension's install hint, as the
  SkyPilot door already does when the extra is missing; nothing else changes.

## Verification

- `tests/scripts/test_carve_platform.py` classifies every tracked path with no `forgotten`
  entry after the `extensions` tree exists; `tests/scripts/test_assemble_public_tree.py`'s
  leak scan reads zero on the public tree with the new category and hits on a planted
  extension import; the new extensions-tree test proves the layout, the scan and the
  generated CI on the assembled output.
- `tests/core/test_architecture_boundaries.py` rejects the two planted edges (extension →
  builtin, extension → platform) and holds on the tree; `test_public_core_boundary.py`
  rejects a root extra that names an extension wheel.
- The extensions CI's `python` job passes against the released `cairndev` with
  `tests/dataflow_conformance` green for each adapter; the nightly against the public `main`
  is wired and has run once.
- The platform's CI (#2514) installs the extension wheels from PyPI and the `campaign`
  profile's `cairn doctor` run resolves `metaflow`.
- The first release: three PyPI names reserved, one tag per wheel, `pip install
  cairn-operator-<engines>[metaflow]` on a clean `cairndev` venv registers the engine
  (`cairn doctor` lists it) and `check_public_set.py` refuses that same venv for the public
  job.

## Alternatives rejected

- **Keep the adapters in the core behind extras.** Every connector in core is a matrix cell
  to keep green (ADR-0127, "More connectors in core"), and the public CI could no longer
  prove the core installs alone with nothing heavy.
- **Put the adapters in the platform.** They are public today and their users are not the
  platform's; an Apache-2.0 adapter in a private repository is a regression of ADR-0120.
- **A `cairn-ext-*` prefix.** It would encode the repository into the name and break the
  rule that identity survives a move; the repository README already commits to
  `cairn-operator-*`.
- **Develop the extensions in their own repository from day one, with the monorepo depending
  on their wheels.** Two trees to keep in step while the engine substrate (#2583) is still
  moving under them; revisit once the first extension has released and the substrate is
  stable (owner decision 1).
- **A repository-wide version.** Forces a release of every wheel for one adapter's fix and
  says nothing about which `cairndev` range each supports; per-wheel `[compat]` already
  carries that.

## Decisions the owner takes on acceptance

1. **Source home before the first release.** Recommendation: the monorepo, a third tier
   `cairn/operators/extensions/`, assembled out (D4); the alternative is developing in
   `cairn-extensions` directly and having the monorepo depend on its wheels.
2. **The engines wheel's shape.** Recommendation: one wheel, `cairn-operator-dataflow-engines`
   (import `operator_dataflow_engines`), with the three extras `[prefect]`, `[metaflow]`,
   `[skypilot]` as today, because `engines_skypilot.py` imports `engines_metaflow` and
   three wheels would make that an extension → extension edge the boundary rule forbids.
   The alternative is three wheels once #2583 also promotes what SkyPilot borrows from
   Metaflow.
3. **An extension's install dependency on a builtin wheel.** Recommendation: allowed as a
   distribution dependency when the extension's leg needs the builtin's operation present at
   run time (the dataflow pack's `[ssh]` extra already names `cairn-operator-remote[ssh]`),
   never as an import; the import rule stays as written in D5.
4. **The flow export's Prefect/Metaflow code generation** (`cairn/core/dataflow/export.py:54`).
   Recommendation: stays in core for now (it emits text, imports nothing, and
   `tests/core/test_flow_export.py` proves it on the public set); moving it behind registry
   metadata is the ADR-0120 D5 reading and can follow #2583 as its own issue.
5. **The `campaign` profile** (`cairn/profiles/campaign.toml`, selects `metaflow` and
   `temporal`). Recommendation: it moves to the platform's profiles with the carve, since it
   also binds `s3/put` from the private `cairn-operator-s3`; the public profiles stay
   `sandbox`, `secure`, `local-dev` (ADR-0120 D1) and whatever hello-cairn ships.
6. **`secrets/infisical`'s proof.** Recommendation: the `infisical` compose profile is one
   cell of the ADR-0127 D1 matrix (`hello-infisical.toml` on nightly-ops), so the builtin is
   proven and not an extension; otherwise D1's rule would send it here.
7. **The unproven core services** (Qdrant driver and the `rag-qdrant`/`rag-pinecone` extras,
   the Presidio cascade behind `pii`, the `rag` extra's readers). Recommendation: out of this
   record's scope — they are services, not packs, and no entry-point group exists to move a
   service through; a separate decision either cuts them from the public tree or adds a
   provider group (a CLAUDE.md §5 change). Until then they stay as they are and D2 marks
   them undecided.
8. **The `optuna` extra.** Recommendation: it leaves the public root `pyproject.toml` with
   the platform (the assembler's `PRIVATE_EXTRAS` gains it; the carve's `ENGINE_EXTRAS`
   loses it and the platform declares `optuna` itself), since nothing public consumes it.
9. **Release cadence and tags.** Recommendation: per-wheel tags `<distribution>/vX.Y.Z`
   (D3); the alternative is a repository-wide version.
