# ADR-0137 — `cairndev` carries the builtin operator packs and the core contracts: five PyPI projects, not thirty-two

- **Status:** ACCEPTED 2026-10-19 by the owner (option 1, then "Accepted. Go on" with every
  recommendation of "Decisions for the owner", 1–11, including 10a: the narrow amendment of
  ADR-0111 P1 for `cairndev` only).
- **Date:** 2026-10-19
- **Issue:** #2430 (the flip and the first PyPI release).
- **Amends:**
  - ADR-0120 D1: the public distribution set (the builtins and the core contracts are part of
    `cairndev`, not separate distributions).
  - ADR-0121 D2, D3 and D6: `packages/` keeps the builtin and contract sources but they are not
    distributions; the names `cairn-operator-<builtin>` and `cairn-contracts-<core>` are not
    published; what the `cairndev` wheel carries.
  - ADR-0123 D6: the builtins ship inside `cairndev`; their distribution names are retired.
  - ADR-0127 D4 and D8: `train-local` ships inside `cairndev` (its training stack stays
    `cairndev[train]`); the core contracts ship as descriptors inside `cairndev`.
  - ADR-0132 D4 and D5: the wording "the builtin wheels at the same version" and "31 wheels".
  - ADR-0134 D1: `cairndev`'s base dependencies no longer list the builtin tier; the builtin
    tier is inside the wheel. D1's optional tier and D2–D6 are unchanged.
  - ADR-0111 P1 (the 2026-10-05 production design, "exactly one descriptor per distribution",
    "editable contract distributions are unsupported"), for the engine distribution only (D3).
    ADR-0111 belongs to the contract track (owner decision 10 below).
  - The counts in ADR-0127 Consequences ("fourteen builtins and six public contracts in the
    assembler's lists") and ADR-0135 Consequences ("32 packages").
- **Keeps:** the monorepo layout (nothing moves on disk), the import names (`cairn`,
  `operator_<name>`), operator refs, entry-point group names and entry-point names, every
  `pack.toml`, the contract descriptor format `cairn.contracts/1` and every contract's bytes,
  the platform's private contract and operator wheels, `cairndev-sh/cairn-extensions`
  (ADR-0128, dormant), ADR-0134 D1's optional tier, ADR-0132 D1–D3 and D6–D9.
- **Lands after** the in-flight `oss/cutover-b` branch (ADR-0136): it changes the same carve and
  CI-installer code (D6).

## Context

1. The v0.1.0 release stopped on PyPI's "429 Too many new projects created" after 4 of 32
   projects: `cairn-client` 0.1.0 and `cairn-contracts-{artifact,audit,data}` 1.0.0 are on PyPI.
   `cairndev` 0.1.0 is not, so no engine version is burned.
2. The 32 are `cairndev`, `cairn-client`, 14 builtin operator packs, 2 optional operator packs,
   13 core contract distributions and `cairn-pack-hello`
   (`scripts/oss/assemble_public_tree.py:44-56`, `rewrite_pyproject` at `:1026-1095`).
3. 27 of the 32 are base dependencies of `cairndev` (`pyproject.toml:78-118`). No user installs
   one alone, nothing outside the root depends on any of them by name (the only sibling
   reference is the dataflow pack's `ssh = ["cairn-operator-remote[ssh]"]`), and they already
   release in lockstep with the engine (ADR-0132 D5). They are 27 PyPI projects that carry no
   independent choice for a user.
4. Every builtin pack has empty base dependencies; their heavy dependencies sit behind pack
   extras: `dataflow[engine]` (polars, duckdb, pyarrow), `remote[ssh]` (asyncssh),
   `rag_rerank[cross_encoder]`, `rag_rerank[cohere]`, `secrets_infisical[infisical]` and
   `train_local[train]` (torch, transformers, peft, datasets). `train_local` imports none of its
   stack at module level and raises `TrainStackMissing` with an install hint at call time
   (`operator_train_local/finetune.py:28,36-42`). Folding the builtins adds no dependency to a
   base install.
5. The builtins contribute three entry-point groups and one script: `cairn.operators` (22
   operators), `cairn.dataflow_engines` (`builtin`), `cairn.dataflow_stages` (`noop`,
   `noop.slow`, `train.lora`) and `cairn-dataflow-run`. Each pack force-includes its
   `pack.toml` beside its import package. Nothing in a builtin registers `cairn.run_hooks`,
   `cairn.packs` or `cairn.providers`.
6. The operator loader, the manifest lookup and `cairn pack check` key on entry-point names and
   import modules, never on distributions (`cairn/core/operator/loader.py:83`, `:222-257`,
   `cairn/core/pack/drift.py:197-207`). Contract discovery keys on distributions
   (`cairn/core/pack/contracts.py`), D3.
7. Contract discovery today (`contracts.py`, read 2026-10-19):
   - `_descriptor` (`:159-166`) refuses a distribution that inventories more than one
     `contracts.toml`.
   - `_inspect` (`:97-120`) takes the contract name from the distribution metadata
     (`dist.metadata["Name"]`, `:98`). It requires the descriptor path to be
     `cairn_contracts/<that name>/contracts.toml` (`:99-101`) and the descriptor's
     `[distribution].name` to equal it (`:112-114`). The version is read from the descriptor's
     `[distribution].version` but must equal the installed metadata version (`dist.version`,
     `:115-117`).
   - `_discover` (`:169-190`) keys duplicates and definition owners by that name.
   - `_data_only` (`:466-478`) refuses a contract distribution that has entry points, is an
     editable install, or inventories any path outside `cairn_contracts/<name>/` and its
     `.dist-info`. `_runtime_requirements` and `_check_dependencies` (`:481-495`, `:597-608`)
     require its `Requires-Dist` to be exact pins equal to the descriptor's dependencies.
   - References may cross only into the package itself and its declared dependencies (`:619`).
   - What the launch door records (`registry.py:179-194`, `ContractResolution` /
     `ContractDefinitionRef` in `engine/bundle_pipeline.py:116-166`) is the descriptor's name,
     the version and SHA-256 digests. Today the descriptor values and the distribution values
     are the same strings.

   `cairndev` would fail four of these checks if it carried the descriptors: the layout
   (`:100`), the version (`:116`), "exactly one descriptor" (`:165`) and "data-only"
   (`:467-478`).
8. `uv build` builds the sdist first and the wheel from it. A hatch `force-include` of a path
   the sdist excludes fails the wheel build (probe of 2026-10-19). The public root's sdist
   excludes `/packages` today (`assemble_public_tree.py:1085`).
9. Hatch's editable install puts a `.pth` entry for each `packages` root (live source) and
   COPIES every `force-include` file into `site-packages`, listed in `RECORD` (probe of
   2026-10-19; the shared venv shows the same for `operator_mcp/pack.toml`).

## Decision

**D1 — The tier rule: a public distribution exists only where a user makes a choice.**
- `cairndev` is the engine plus:
  - the whole builtin tier (every pack under `cairn/operators/builtins/`, `train_local`
    included, with its stack behind `cairndev[train]`);
  - every core contract package (every `contracts/<name>` the assembler's `CONTRACTS` lists:
    a contract whose provider ships in the core ships in the core, ADR-0120 D1).
- Separate public wheels exist only for:
  - `cairn-client` (a client with no `cairn` dependency, installed without the engine);
  - the optional tier (`cairn/operators/optional/`: `cairn-operator-dataflow-metaflow`,
    `cairn-operator-dataflow-skypilot`, behind `cairndev[metaflow]` / `cairndev[skypilot]`,
    ADR-0134);
  - the example pack `cairn-pack-hello`.
- Unchanged: `cairndev-sh/cairn-extensions` wheels (own wheels, own releases, ADR-0128); the
  platform's official operator packs, domain packs and private contracts (eval, dataset,
  dataset-stages, ingest) stay separate wheels on the platform's private index, never PyPI.
- **The PyPI project list:** `cairndev`, `cairn-client`, `cairn-operator-dataflow-metaflow`,
  `cairn-operator-dataflow-skypilot`, `cairn-pack-hello`. Five projects, of which
  `cairn-client` exists; the first complete release creates four.
- A future builtin or core contract joins `cairndev`. A pack that a user would install
  separately (a heavy dependency, an optional engine) goes to the optional tier.

**D2 — The monorepo is unchanged; the assembled public tree builds one `cairndev` wheel.**
- **The monorepo** keeps every builtin and contract as its own workspace member (separate
  distributions, contracts installed non-editable). Nothing moves on disk. The monorepo is
  frozen at the flip (ADR-0132 D2), so the fold is the assembler's job, and after the flip it
  is the public repository's own configuration.
- **The public tree keeps the sources where they are** (`packages/cairn-operator-<name>/` and
  `packages/cairn-contracts-<name>/`). Their directories keep their names, so `tests/_layout.py`,
  the boundary tests and the path rewrites are unchanged. Each folded pack keeps its
  `pyproject.toml` as the declaration of its entry points and extras, with `[build-system]`
  removed and a header saying it is folded into `cairndev` (owner decision 5). Nothing builds,
  installs or publishes it.
- **The public root `pyproject.toml`** (written by `rewrite_pyproject`):

  ```toml
  [project]
  name = "cairndev"
  dependencies = [ "cairn-client", ...third-party... ]   # no cairn-operator-*, no cairn-contracts-*

  [project.optional-dependencies]                        # the folded packs' extras, flattened
  ssh = ["asyncssh>=2.14"]                               # was cairn-operator-remote[ssh]
  train = ["torch>=2.11,<2.12", "transformers>=5.5,<5.6", "peft>=0.20,<0.21", "datasets>=4.3,<4.4"]
  infisical = ["infisicalsdk>=1.0.0"]                    # unchanged
  dataflow-engine = ["polars>=1.0", "duckdb>=1.0", "pyarrow>=15"]       # was cairn-operator-dataflow[engine]
  rerank-cross-encoder = ["sentence-transformers>=3.0"]  # was cairn-operator-rag-rerank[cross_encoder]
  rerank-cohere = ["cohere>=5.0"]                        # was cairn-operator-rag-rerank[cohere]
  metaflow = ["cairn-operator-dataflow-metaflow", "metaflow>=2.12"]     # unchanged (optional wheel)
  skypilot = [...]                                       # unchanged (optional wheel)
  hello = ["cairn-pack-hello[all]"]                      # unchanged

  [project.scripts]                                      # + the dataflow pack's script
  cairn-dataflow-run = "operator_dataflow.engine.entry:main"

  [project.entry-points."cairn.operators"]               # the union of the 14 packs' tables
  "mcp/call" = "operator_mcp...."                        # ...22 entries, values verbatim
  [project.entry-points."cairn.dataflow_engines"]
  builtin = "operator_dataflow...."
  [project.entry-points."cairn.dataflow_stages"]
  noop = "..."; "noop.slow" = "..."; "train.lora" = "..."

  [tool.hatch.build.targets.wheel]
  packages = ["src/cairn",
              "packages/cairn-operator-mcp/src/operator_mcp", ...]          # 1 + 14 roots

  [tool.hatch.build.targets.wheel.force-include]
  "packages/cairn-operator-mcp/pack.toml" = "operator_mcp/pack.toml"        # x 14
  "packages/cairn-contracts-dataflow/definitions" = "cairn_contracts/cairn-contracts-dataflow"   # x 13

  [tool.hatch.build.targets.sdist]   # must carry what the wheel force-includes (Context 8)
  exclude = [..., "/packages/cairn-client", "/packages/cairn-operator-dataflow-*",
             "/packages/*/tests", "/docs", "/tests", "/deploy", "/examples"]

  [tool.uv.workspace]
  members = ["packages/cairn-client", "packages/cairn-operator-dataflow-metaflow",
             "packages/cairn-operator-dataflow-skypilot", "examples/hello-cairn"]
  [tool.uv.sources]                  # the 27 folded names leave; the 4 members stay
  ```

  - The entry-point, script and extra tables are copied verbatim from the folded packs' own
    `pyproject.toml`. A public test holds the root equal to their union (D5).
  - The extra names `ssh`, `train` and `infisical` exist on the root today and keep their
    meaning. The three new names are owner decision 6.
- **Dev and release use the same configuration.** A contributor's `uv sync` installs `cairndev`
  editable: each `operator_<name>` is live source through the `.pth`, and every `pack.toml` and
  contract file is a copy in `site-packages` (Context 9). D3 accepts that copy. Editing a
  contract file needs `uv sync --reinstall-package cairndev`, as editing a non-editable
  contract member needs a reinstall today. `uv build --all-packages` builds five wheels from
  the same file.
- **What the `cairndev` wheel carries** (amends ADR-0121 D6): `cairn/`, the 14 `operator_<name>`
  packages, each with its `pack.toml`, and `cairn_contracts/<package>/` for the 13 contract
  packages, plus LICENSE, NOTICE, THIRD_PARTY_NOTICES and `py.typed` as before. The packs'
  tests, DESIGN.md, README and CLAUDE/AGENTS files sit outside the package roots or are
  excluded, so they are not carried.

**D3 — Contract discovery: the engine distribution may host several contract packages.**

A *contract package* is one descriptor directory `cairn_contracts/<package>/contracts.toml`
with the files beneath it. Its identity is the descriptor's `[distribution]` table (`name`,
`version`), unchanged in format (`cairn.contracts/1`) and in bytes. A distribution *hosts*
packages in one of two modes:

- **Standalone (today's contract distribution, unchanged).** Exactly one package, whose
  directory name and descriptor name equal the distribution's name and whose descriptor
  version equals the installed version. The distribution is data-only, has no entry points,
  is not editable, and its `Requires-Dist` equal the descriptor's dependencies as exact pins.
  The platform's private contracts, extensions' contracts and the monorepo's members keep this
  mode.
- **Engine-hosted (new).** Only the distribution named `ENGINE_DISTRIBUTION`
  (`cairn/core/pack/loader.py:86`, `cairndev`, read lazily so `contracts` stays off the loader's
  import path) may host packages it is not named after, and it may host any number.
  For each hosted package:
  - **Identity from its own descriptor.** The directory name equals the descriptor's
    `[distribution].name`. The version is the descriptor's own and is not compared with
    `dist.version`.
  - **Data-only per package.** Every path inventoried under `cairn_contracts/<package>/` has
    no executable or `.pth` suffix (the `_EXECUTABLE` set). The host's own code, entry points
    and `Requires-Dist` are not judged; it is the engine.
  - **Editable host accepted.** An editable host is accepted because every descriptor and
    definition must still be inventoried in the host's `RECORD` and resolve inside its install
    root (`_asset`, `:86-94`). That holds for hatch's editable copies and never for a file on
    a live source path.
  - **Dependencies.** A hosted package's `[[dependencies]]` must be installed as packages at
    the exact version; the `Requires-Dist` equality is not applied.
- **Kept for every package, whatever its host:**
  - one owner per definition id, keyed by package name;
  - a package name installed twice (for example a standalone `cairn-contracts-audit` beside
    the one in `cairndev`) is refused, and the message names both hosts and the remedy
    (uninstall the standalone one; owner decision 4);
  - references cross only into the package and its declared dependencies;
  - acyclic dependencies;
  - digests, evidence and the process cache (`_installed_key`, `_stamps`, which already walk
    each descriptor's directory).
- **Refused, as today:** a distribution other than the engine that carries more than one
  descriptor, or a descriptor not named after it.
- **What does not change:**
  - `discover_contracts`, `resolve_contracts`, `installed_contract_bodies`, the registry
    facades and the launch door (`dispatch/pipeline_launch.py:220-311`, keyed by
    `contract://` URI) are unchanged in signature and behaviour.
  - `ContractDistribution` and `ResolvedDistribution` stay one record per package (`name` =
    package, `version` = descriptor version). No field is added.
  - The recorded `ContractResolution` for a package is byte-for-byte the same whether it is
    installed standalone or hosted, so a leg admitted before the upgrade still passes
    `invocation_contract`'s digest check after it (`registry.py:234-260`, ADR-0111 §4).
- **Why only the engine.** ADR-0111 keeps contracts independent of their providers so that a
  third party cannot ship the contract it is judged against. The engine is the root of trust:
  it runs the gate that checks the contracts. Every other distribution keeps the standalone
  rule. ADR-0111's "avoid a mandatory global contract megabundle": the 13 packages were already
  mandatory base dependencies of `cairndev`; the fold changes the packaging unit, not what is
  mandatory, and each package keeps its own identity and semver.
- **ADR-0118's "stage contracts in their own distribution":** no public contract has a `stage`
  definition, and the reason (an older core refuses an unknown kind) cannot arise for packages
  that ship with the core that reads them.

**D4 — Versioning.**
- `cairndev`, `cairn-client` and the two optional wheels release in lockstep (ADR-0132 D5).
  `cairn-pack-hello` keeps its own version (0.2.0 today), as it already does.
- Builtins version with the engine. Their `pack.toml` versions and operator versions are
  unchanged; `[compat] cairn_engine` is checked against `cairndev` as before.
- Contract packages keep their own semver inside their descriptors (`cairn-contracts-dataflow`
  1.1.0, the others 1.0.0). A contract change is a new descriptor version or a new definition
  revision. It is never an edit of published bytes: the recorded digests would refuse it.
- The engine's version and a contract package's version are independent. A `cairndev` release
  that leaves a descriptor unchanged leaves that package's recorded identity unchanged.
- **ADR-0121 D3 amended:** `cairndev`, `cairn-client`, `cairn-pack-*` and `cairn-operator-*`
  (optional tier, extensions, the platform's official packs) and `cairn-contracts-*`
  (standalone contract wheels) are distribution names. A builtin pack and a core contract
  package have no distribution of their own, and their former distribution names are not
  published. `cairn-contracts-<core>` remains the contract package's name inside descriptors
  and recorded resolutions.
- **The PyPI orphans:**
  - `cairn-contracts-artifact`, `-audit` and `-data` 1.0.0 stay on PyPI. They are never
    installation instructions. Installing one beside `cairndev` is refused by D3 (duplicate
    package) and by `scripts/check_public_set.py` (D5).
  - `cairn-client` 0.1.0 is not an orphan: `cairn-client` remains a public project.
  - Owner decisions 7–9.

**D5 — Where "one pack = one distribution" is assumed, and what replaces it.**

The rule: a builtin pack's identity is its import package (`operator_<name>`, the entry
point's module root) and its `pack.toml`; a contract package's identity is its descriptor.
Neither is a distribution. Found 2026-10-19 by searching for `distribution(`,
`installed_distributions`, `metadata.version`, `PackageNotFoundError`, `ep.dist`,
`packages_distributions`, `cairn-operator-` and `cairn-contracts-` in `cairn/`, `scripts/`,
`tests/`, `Dockerfile.*` and `deploy/`.

| Where | Assumption | Change |
|---|---|---|
| `cairn/core/pack/contracts.py:97-120,159-190,466-495,597-622` | a contract's name/version is its distribution's; one descriptor per distribution; host is data-only | D3 |
| `tests/core/test_builtins_complete.py:36-39` | maps `ep.dist.name` → `cairn-operator-<pack>`; under the fold every builtin's dist is `cairndev`, so `PACK_OF` is empty and the guard fails | map by the entry point's module root `operator_<pack>` |
| `cairn/operators/builtins/dataflow/tests/test_runner_kernel_versions.py:19,25,36` | `version("cairn-operator-dataflow")` raises `PackageNotFoundError` | compare with `stage_version("noop")` |
| `tests/_platform_layout.py:97-101` (platform tree) | finds the core contracts as installed `cairn-contracts-*` distributions | read them with `discover_contracts()` |
| `Dockerfile.platform:192-196` | `len({e.dist.name for e in cairn.operators eps}) >= 36` (13 builtins + 23 official); folded: 24 | count distinct module roots, and assert the 14 builtins come from `cairndev` |
| `Dockerfile.engine:327-339` (public verify via `public_dockerfile`, `assemble_public_tree.py:501-513`) | eps ≥ 14 and the 13 `cairn-contracts-*` names resolve | values hold (package names unchanged); add: every builtin ep's dist is `cairndev`, no `cairn-operator-<builtin>`/`cairn-contracts-<core>` dist installed |
| `scripts/check_public_set.py:44-47,69-82` | the public set is the root's base dependencies (27 sibling names today) | after the fold a folded name installed is private by construction; name the reason ("folded into cairndev, ADR-0137"); update the docstring (`cairn-sdk`, "builtin operator packs") |
| `scripts/oss/carve_platform.py:442-448` (`public_dists`) and `:592-610` (pins) | 32 public distributions; `public-core` pins 31 | 4 distributions; translate folded requirements in carried extras (`engines`: `cairn-operator-dataflow[engine]` → `cairndev[dataflow-engine]`) |
| `scripts/oss/platform/circleci/install_core_main.py:54-63,96-98` (on `oss/cutover-b`) | every `packages/*/pyproject.toml` with `[build-system]` is a core distribution to install | holds once folded packs carry no `[build-system]` (D2); a test pins it |
| `scripts/oss/assemble_public_tree.py:285-286,1042-1095,1230,342` | contracts and builtins placed as members/sources/deps; sdist excludes `/packages`; quickstart and CHANGELOG text say "sibling packages" | D2 |
| `cairn/cli/commands/ls.py:236-290` | the operator map's "wheel" column and its kernel↔operator correlation by distribution | unchanged code; the column reads `cairndev` for builtins (cosmetic); correlation still matches by leaf name |
| `cairn/core/dataflow/defs.py:169-173,199`, `runner.py:534-538` | `kernel_version` and the stage catalog's `provider` come from the stage's distribution | unchanged code; `provider` reads `cairndev`; `kernel_version` is the engine version, which equals the old pack version under lockstep |
| `cairn/cli/commands/lock.py:97-105,130-135` | a source-tree pack's `distribution`; the count of installed pack wheels | unchanged: the lock walks the engine's source tree, which holds no builtin in either tree; the count is a message |
| `cairn/cli/commands/pack.py:233-245`, `cairn/core/operator/manifest_sync.py:256-261,287` | a pack's own `pyproject.toml` declares its name and `cairn.operators` table | unchanged: the folded pack keeps that file (D2) |
| `tests/core/test_distribution_names.py:58-70` | every operator pack directory has a `cairn-operator-<dir>` pyproject | unchanged (D2) |
| install hints: `operator_dataflow/engines_ssh.py:163` (`pip install 'cairn-operator-dataflow[ssh]'`), `operator_train_local/finetune.py:28` and its test `test_train_local_base.py:89,97`, `cairn/core/profile_backends.py:117,181,210` (`cairn-operator-dataflow[engine]`) | name a pack distribution's extra | name `cairndev[ssh]`, `cairndev[train]`, `cairndev[dataflow-engine]` (valid in the monorepo too once its root declares `dataflow-engine`) |
| `tests/scripts/test_assemble_public_tree.py:30-35,121-124,246,426-446`, `test_carve_platform.py:142-211,377`, `test_check_public_set.py:16-46` | per-distribution members, sources, pins | follow D2/D6 |

Unaffected (checked): the operator loader and manifest lookup (`loader.py:83,222-257`, module
roots), `cairn pack check` (`drift.py:197-207`, entry-point names),
`deploy/stage_venv_overlay.py` (each distribution's own `RECORD`),
`tests/core/test_stage_kernels_control_plane.py:44-58` (`packages_distributions` maps the
folded packages to `cairndev`, inside the control plane), `cairn/core/registry_install.py:319`
(domain-pack installs, unsupported).

**D6 — The release, the platform pin and the cutover.**
- **Release** (`public_release`, `assemble_public_tree.py:612-684`):
  - `uv build --all-packages` builds five wheels.
  - The publish step uploads `cairndev` first, then `cairn-client`, the optional wheels and
    `cairn-pack-hello`, as separate `uv publish` calls on explicit files. Today one call on
    `dist/*` uploads `cairndev` last, so a 429 on the fourth new project would again leave the
    release without its engine. `--check-url` keeps a rerun idempotent.
- **CI:** the generated `ci.yml` gains a built-wheel step. It builds the wheels, installs
  `cairndev` from `dist/` into a clean venv (D7's gate), runs `cairn pack check`, resolves the
  13 contract packages and explains a hello-cairn pipeline. The editable dev venv is not the
  shape users install (owner decision 11).
- **The platform pin** (ADR-0132 D4, carve `--core-source pypi`) becomes:
  - `cairndev[<engine extras>]==X.Y.Z`;
  - the `public-core` group: `cairn-client==X.Y.Z` and the optional wheels the platform uses
    (`cairn-operator-dataflow-metaflow==X.Y.Z` through `cairndev[metaflow]`).

  The carve stops naming the 27 folded distributions. The `wheels` mode stages five wheels in
  `public-wheels/`. The platform's private contract and operator wheels are unchanged and
  depend on no folded name (checked: no pack `pyproject.toml` outside the root names a folded
  distribution, apart from the dataflow pack's own `ssh` extra).
- **`oss/cutover-b` (ADR-0136, in flight)** changes `install_core_main.py`, the carve and the
  prod tooling. This record lands after it, and its implementation rebases on it. If
  `cutover-b` is merged and the platform is carved before this fold is released, the platform
  pins 32 names that will never all exist on PyPI. So the order is: cutover-b merged → this
  fold → the first complete PyPI release → the platform's pin moves to PyPI (ADR-0132 D9
  steps 4–5).

**D7 — The acceptance gate (before any upload).**
1. Assemble a fresh tree from `main`. `uv lock` succeeds; the leak scan reports 0.
2. `uv build --all-packages` produces exactly five wheels and five sdists: `cairndev`,
   `cairn_client`, `cairn_operator_dataflow_metaflow`, `cairn_operator_dataflow_skypilot`,
   `cairn_pack_hello`.
3. In a clean venv, `uv pip install --no-index --no-deps --find-links dist cairndev
   cairn-client cairn-pack-hello`, then the third-party requirements from
   `uv export --frozen --no-emit-workspace --extra hello`. The only `cairn*` distributions
   installed are those three (`scripts/check_public_set.py --extras hello` passes).
   `--no-index` covers the first step only: the third-party wheels come from the index.
4. In that venv:
   - `cairn pack check` exits 0;
   - `resolve_contracts()` returns the 13 core packages, each hosted by `cairndev`;
   - 22 `cairn.operators` entry points, all from `cairndev`;
   - `cairn-dataflow-run --help` runs;
   - `CAIRN_PACK_PATH=examples/hello-cairn cairn pipeline explain
     hello-cairn/profile-and-publish` runs, and one hello-cairn pipeline runs in process.
5. The public tree's `ci.yml` `public` and `public-e2e` jobs pass on the assembled tree, and
   `Dockerfile.engine` builds with its verify steps.
6. The platform carve against the five wheels: its ship guard and `Dockerfile.platform`'s
   operator inventory pass. The hello-cairn feature proof passes on the box (57/57).

## Consequences

- The first complete PyPI release creates four projects (`cairndev`, the two optional wheels,
  `cairn-pack-hello`), uploaded engine first.
- `pip install cairndev` installs one cairn distribution plus `cairn-client`, the same code as
  before, and the same entry points and contract identities.
- The monorepo is unchanged until it freezes. The public repository carries the fold as its own
  configuration, held by a test that the root's entry points, scripts, extras, wheel packages
  and force-includes equal the union of the folded packs' declarations.
- A builtin added after the flip is a directory under `packages/` plus root `pyproject.toml`
  lines; the test names any missing line.
- Contract discovery gains one host mode, limited to the engine. Third-party contracts keep the
  ADR-0111 rules unchanged.
- The `cairn_contracts/<package>/` directories of a standalone wheel and of `cairndev` share
  one `site-packages` path. Installing an orphan over `cairndev` overwrites the same files, and
  uninstalling it removes the engine's copies. D3 refuses the duplicate and
  `check_public_set` names it before that state reaches a run.
- `cairn ls` shows `cairndev` as the wheel of every builtin operator, and the console's stage
  catalog shows it as the provider of `noop`, `noop.slow` and `train.lora`.
- Install hints name `cairndev[...]` extras everywhere, which is correct in both trees.
- What this does not change: `Operation`, `pack.toml`, `PipelineDef`, `DataflowSpec`, `Run`,
  `console/openapi.json`, the descriptor format, any recorded identity, any chokepoint's
  position. The contract-discovery change (D3) changes the rules of a governance input; it is a
  CLAUDE.md §5 item and needs the owner's approval with this record.

## Verification

- `tests/core/test_contract_discovery.py` (D3):
  - **Accepting:**
    - an engine host with two packages gives two records, each with its descriptor's name
      and version, not the host's version;
    - an editable engine host whose descriptors are inventoried copies resolves;
    - an engine host with entry points and non-pinned `Requires-Dist` resolves;
    - a hosted package depending on another hosted package at its exact version resolves;
    - one package installed standalone and then hosted gives the same
      `contract_resolution_snapshot()` entries, and `invocation_contract` accepts a closure
      recorded under one and read under the other.
  - **Refusing:**
    - a non-engine distribution with two descriptors;
    - a non-engine distribution with code plus a descriptor named after itself (unchanged);
    - a hosted directory name different from its descriptor name;
    - a `.py` or `.pth` under a hosted package;
    - a hosted descriptor or definition not in the host's `RECORD`;
    - a hosted asset that escapes the install root through a symlink;
    - one package name standalone and hosted (the message names both hosts);
    - one definition id in two hosted packages;
    - a hosted dependency missing, at the wrong version, or cyclic;
    - an editable standalone contract distribution (unchanged);
    - a standalone descriptor version different from its metadata (unchanged).
- `tests/core/test_contract_resolution_cache.py`: the cache invalidates when the engine host
  is upgraded (its `RECORD` stamp).
- A new public test `tests/core/test_folded_engine_wheel.py`:
  - the root's entry points, scripts and extras equal the union of the folded packs'
    `pyproject.toml` tables;
  - every folded pack's import root is in the wheel `packages`, and its `pack.toml` and every
    core contract's `definitions` are in `force-include`;
  - no folded directory is a workspace member or has a `[build-system]`;
  - the sdist does not exclude a force-included path.
- `tests/core/test_builtins_complete.py` and `test_runner_kernel_versions.py` pass in both
  layouts after D5's changes. `tests/scripts/test_assemble_public_tree.py`,
  `test_carve_platform.py` and `test_check_public_set.py` cover D2/D6. A rejecting fixture:
  `cairn-contracts-audit` or `cairn-operator-mcp` installed in the public venv is refused,
  naming ADR-0137.
- D7's gate, run once before the first upload, and its built-wheel step on every public CI run.

## Alternatives rejected

- **Publish all 32 over several days, inside PyPI's rate limit.** It works, and every future
  builtin or contract costs a new project and a rate-limit window. It also keeps 27 projects
  that no user installs alone and that can only release in lockstep. ADR-0132 D5 already
  treats them as one release.
- **Fold only the contracts** (19 projects) **or only the builtins** (18). Either keeps the
  rate-limit problem at half size. The contracts alone still need D3, and the builtins alone
  gain nothing a user can choose.
- **Fold only in the release build** (dev keeps per-pack distributions, a build hook or a
  second configuration builds the fat wheel). It tests one shape and ships another. A hook is
  new build machinery, and every member would install twice in a dev venv (once editable
  through `cairndev`, once as itself), which duplicates every contract package.
- **Keep the descriptors' identity tied to the host distribution** (contract name =
  `cairndev`, version = engine version). It changes every recorded `ContractResolution`, breaks
  `invocation_contract`'s digest check for runs admitted before the upgrade, couples contract
  semver to the engine, and merges 13 cohesive packages into one owner.
- **Let any distribution host several contract packages.** It reopens what ADR-0111 closed: a
  provider wheel shipping the contract it is judged against.
- **Move the builtins into `src/cairn/operators/`.** A file move in both trees, new import
  paths for 14 packs, and an edit of every test and doc path, for the same wheel.
- **Rename `packages/cairn-operator-<builtin>` to `packages/builtins/<name>`.** The names are
  harmless and every layout helper, rewrite and test reads them. Renaming them is churn, with
  no change to what ships.

## Decisions for the owner

1. **The tier rule and the five-project list (D1).** Recommendation: accept. `cairndev`,
   `cairn-client`, `cairn-operator-dataflow-metaflow`, `cairn-operator-dataflow-skypilot`,
   `cairn-pack-hello`.
2. **Who may host several contract packages (D3).** (a) only `cairndev`; (b) any distribution.
   Recommendation: (a). It keeps ADR-0111's independence for every third party.
3. **An editable engine host (D3).** (a) accept it when every descriptor file is an inventoried
   copy inside the install root; (b) keep refusing editable installs and make contributors
   install `cairndev` non-editable. Recommendation: (a). The public CI's `uv sync` is editable,
   and (b) would make every contract-reaching test refuse in the public repository.
4. **A package installed both standalone and hosted.** (a) refuse, with a message naming both
   hosts and the uninstall; (b) accept when the descriptor and definition digests are identical.
   Recommendation: (a). It fails closed, the files collide in `site-packages` anyway, and only
   the three orphans can cause it.
5. **The folded packs' `pyproject.toml` in the public tree.** (a) keep it, without
   `[build-system]`, as the pack's declaration; the root copies it and a test holds them equal;
   (b) delete it, and teach `manifest_sync.check_pack`, `cairn pack`, the layout helper and
   `test_distribution_names` to read the root. Recommendation: (a). It needs no core code
   change, and `install_core_main.py` already skips a pyproject without `[build-system]`.
6. **Names for the three new root extras.** (a) `dataflow-engine`, `rerank-cross-encoder`,
   `rerank-cohere`; (b) fold `dataflow[engine]` into the existing `kernel` extra.
   Recommendation: (a). `kernel` is what the public image installs, and (b) would add polars,
   duckdb and pyarrow to it.
7. **The first complete release's version.** (a) `0.1.1` for the lockstep set; (b) `0.1.0`,
   relying on `uv publish --check-url` skipping `cairn-client` 0.1.0 when the rebuilt wheel is
   byte-identical to the uploaded one. Recommendation: (a). It does not depend on build
   reproducibility, and `cairndev` 0.1.0 has never been published, so nothing is lost.
8. **The three contract orphans** (`cairn-contracts-{artifact,audit,data}` 1.0.0).
   (a) leave them; (b) yank them after the first `cairndev` release, with the reason "folded
   into cairndev (ADR-0137)"; (c) upload a `1.0.0.post1` README tombstone. Recommendation: (a)
   now and (b) after the release. A yanked file is no longer chosen by an unpinned install, and
   no new upload is needed.
9. **Reserving the 24 folded names never published** (14 `cairn-operator-<builtin>`, 10
   `cairn-contracts-<core>`). (a) do not reserve; (b) reserve a few a day with placeholder
   uploads. Recommendation: (a). No document or lock names them after this record, the platform
   resolves only pinned names, and each reservation costs a project creation against the same
   rate limit. Revisit if a squat appears.
10. **ADR-0111 is the contract track's record** (owner rule of 2026-10-09: that track belongs to
    another session). D3 amends its P1 for the engine host. Recommendation: accept the amendment
    as part of option 1, add "amended by ADR-0137" to ADR-0111's index row on acceptance (left
    unmarked until then), and have PR1 (`contracts.py`) reviewed by, or coordinated with, that
    track.
11. **The built-wheel CI step (D6).** (a) make it a required check of the public repository;
    (b) run it only in the release workflow. Recommendation: (a). Without it, the fold's
    configuration can drift with every check still green.

## Owner's acceptance (2026-10-19)

Every recommendation above, as written: 1 accept the tier rule and the five-project list; 2a only
`cairndev` hosts several contract packages; 3a an editable `cairndev` is a contract host; 4a a contract
package installed both standalone and inside `cairndev` is refused; 5a folded packs keep their
`pyproject.toml` without `[build-system]`; 6a extras `dataflow-engine`, `rerank-cross-encoder`,
`rerank-cohere`; 7a the first complete release is 0.1.1; 8a leave the three orphan contract
projects, yank after `cairndev` is released; 9a no name reservations; 10a ADR-0111 P1 amended for
`cairndev` only; 11a the built-wheel check is a required CI check.
