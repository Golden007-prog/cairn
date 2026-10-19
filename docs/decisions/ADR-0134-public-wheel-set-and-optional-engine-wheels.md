# ADR-0134 — The public wheel set: Metaflow and SkyPilot as optional engine wheels, Prefect dropped, platform content out of `cairndev`

- **Status:** ACCEPTED 2026-10-16 by the owner ("accept ADR-0134, approve PR4a"): D1–D6 as
  written, and the PR4a door design in D3c (`docs/oss/2678-wheel-trim-plan.md`).
- **Date:** 2026-10-16
- **Issue:** #2678 (before the first PyPI release, #2430).
- **Amends:**
  - ADR-0127 D5 and ADR-0128 D2: the three engine adapters do not go to
    `cairndev-sh/cairn-extensions`. Metaflow and SkyPilot become optional wheels in the public
    repository, and Prefect is removed.
  - ADR-0128 owner decision 2: two wheels, not one `cairn-operator-dataflow-engines`.
  - ADR-0128 owner decision 4: the flow export drops its Prefect target.
  - ADR-0081 §3: the engine list loses `prefect`.
  - ADR-0104 D5: the SkyPilot launch check moves from a core module to the engine, still
    fired before any record exists.
  - ADR-0120 D1's public profile list (D5).
  - ADR-0127 D7's placement of Ask's engine wiring (D6).
- **Keeps:**
  - ADR-0132: the extensions repository stays dormant, now with no move scheduled.
  - ADR-0099 §1: a requirement implied by configuration is derived and refused at the launch
    door.
  - ADR-0120 D1: a contract whose provider ships in the core ships in the core.
  - ADR-0127 D1–D4 and D6–D8.

## Context

1. PyPI never accepts a re-upload of a version. Whatever the first `cairndev` release
   contains, every later release must stay compatible with it, or deliberately break it.
2. The `cairndev` wheel built from main 0185b131 still carries content the public core does
   not prove or cannot use (#2678):
   - The builtin dataflow pack registers four `cairn.dataflow_engines`: `builtin`, `prefect`,
     `metaflow` and `skypilot`
     (`cairn/operators/builtins/dataflow/pyproject.toml`). `cairndev` depends on that pack,
     so every install carries about 2,100 lines of adapter code for three engines the
     feature proof never runs:
     - `engines_metaflow.py` (890 lines), `engines_skypilot.py` (1093 lines) and
       `engines_prefect.py` (129 lines);
     - plus `metaflow_bridge.py`, `metaflow_train_bridge.py` and `prefect_bridge.py`.
   - Core names SkyPilot in a launch door of its own (`cairn/core/skypilot_door.py`), called
     from `dispatch/dispatcher.py:890`, `dispatch/pipeline_doors.py:233` and
     `preflight.py:264`.
   - Platform deployment profiles ship in `cairn/profiles/` (D5), and the engine wiring for
     the platform's Ask agent ships in `cairn/ask/` (D6).
3. ADR-0127 D5 and ADR-0128 D2 sent all three adapters to `cairn-extensions`. ADR-0132 keeps
   that repository dormant until they arrive. Moving them now means running a second public
   repository, with its own CI and releases, for two packages. On 2026-10-16 the owner chose
   instead to keep Metaflow and SkyPilot in the public repository as optional wheels, and to
   drop Prefect.
4. #2583, closed on 2026-10-15, promoted the engine substrate into `cairn.core.dataflow`: the
   runner, result readers, stage loader and SSH fetch helpers. The adapters now import only
   `cairn.core.*`, with one exception: `engines_skypilot.py:89-93` imports `_ESSENTIAL_ENV`,
   `aggregate_resources` and `gang_workers` from `engines_metaflow`. All three are pure
   functions of a `DataflowSpec` and the engine's environment allowlist
   (`engines_metaflow.py:643-753`).
5. `check_ready()` already exists on `SkyPilotEngine` (`engines_skypilot.py:746`), and the
   door reaches it only through the registry: `load_engine_factory("skypilot")`, then
   `engine.check_ready()`. What makes the door SkyPilot-specific is a constant
   (`ENGINE = "skypilot"`, `skypilot_door.py:34`), the install hint (`:38`), and the rule that
   an empty target is a refusal (`:89-97`). `check_ready` is not part of the `DataflowEngine`
   protocol (`cairn/core/dataflow/engine.py:76`).
6. Both adapters import their library lazily: `sky` at `engines_skypilot.py:185,209,246`, and
   `metaflow` at `engines_metaflow.py:541,567`. Their tests run without the library installed,
   as they do today.

## Decision

**D1 — The default install ships what the example proves; two engines become optional wheels
in the public repository.**
- `cairndev`'s base dependencies stay the builtin tier (ADR-0127 D5).
- The new tier `cairn/operators/optional/` holds public operator packs that the root offers
  only through extras.
- The assembler places each one as `packages/cairn-operator-<name>` beside the builtins. The
  public tree's own CI runs their tests. They are not base dependencies, and
  `scripts/check_public_set.py` accepts them only when the extra that names them is
  installed.
- The carve treats the tier as public, like the builtins.

**D2 — `cairn-operator-dataflow-metaflow`, behind `cairndev[metaflow]`.**
- It takes `engines_metaflow.py`, `metaflow_bridge.py`, `metaflow_train_bridge.py`, the
  `metaflow` entry point in `cairn.dataflow_engines`, and the ten `test_metaflow_*` files.
- The builtin dataflow pack keeps the `builtin` engine, `dataflow/run`, `ml/run`,
  `artifact/promote`, the `noop` stages and `cairn-dataflow-run`.
- Root extra: `metaflow = ["cairn-operator-dataflow-metaflow", "metaflow>=2.12"]`. The wheel
  itself declares no hard dependency on `metaflow`, so its tests run without it, as today.
- Core never imports it; `tests/core/test_engine_registry.py` already enforces that.
- The platform depends on `cairndev[metaflow]`: its `campaign` profile selects
  `dataflow_engine = "metaflow"`, and the root `engines` extra and `Dockerfile.engine`
  retarget to it.
- `cairn doctor`'s install hint (`doctor.py:212`) names the new extra.
- `docs/features.yaml` lists it as public, with
  `unproved: unit-tested; no feature-proof cell yet`.

**D3 — `cairn-operator-dataflow-skypilot`, behind `cairndev[skypilot]`, with its launch check
supplied by the engine.**

*(a) The wheel.*
- It takes `engines_skypilot.py`, the `skypilot` entry point and `test_skypilot_engine.py`.
- Root extra: `skypilot = ["cairn-operator-dataflow-skypilot",
  "skypilot[aws,kubernetes,runpod,ssh,vast]>=0.13"]`.

*(b) The shared helpers.* The three helpers it borrows from Metaflow move into core as
`cairn/core/dataflow/resources.py`:
- `gang_workers` and `aggregate_resources`;
- the engine environment allowlist (`engines_metaflow.py:730-753`). Core publishes only the
  engine-neutral part: the `CAIRN_*` contract names and prefixes, as
  `essential_env(extra_prefixes=())`. Each engine adds its own prefixes; Metaflow adds
  `METAFLOW_`, `CAIRN_METAFLOW_` and `OBS_METAFLOW_`, so core does not name Metaflow's
  variables. Today SkyPilot forwards those Metaflow prefixes because it borrows the whole
  set. Whether it still needs them is checked in PR2 and kept explicitly if so.

Both wheels import them from there, so neither imports the other (the "operator pack →
another operator pack" rule, `tests/core/test_architecture_boundaries.py`).

*(b2) How the engine packs read configuration.* The Metaflow and SkyPilot factories read about
fifteen `metaflow_*` and `skypilot_*` keys through `_selected` (`engines.py:180-193`). That
helper lazily imports `cairn.core.backend_selection`, an edge recorded as category (C) debt
(`tests/core/test_architecture_boundaries.py:116`), which "fails on a new edge".
- Copying it into the new packs would add two new debt edges. Importing
  `operator_dataflow.engines` would be an operator pack → operator pack edge. Both are
  refused.
- Instead, `cairn.core.operator` re-exports the one-plane read as
  `cairn.core.operator.selected_backend`, the category (A) route that `getenv` already
  takes (`cairn/core/operator/__init__.py:131`). This is additive to the operator surface.
- All three engine packs import it lazily, keeping today's fallback to the environment on
  a data-plane box where core is absent. The builtin dataflow pack's existing (C) row then
  leaves the inventory: the inventory shrinks, it does not grow.

*(c) The launch check.*
- `cairn/core/skypilot_door.py` is replaced by an engine-neutral door,
  `cairn/core/dataflow/engine_door.py` (`check_flow_engines(kinds=…)`).
- **Scope: only the kinds a launch's legs actually reach.**
  - The pipeline door already narrows by `engine_kinds(by_op)` (`pipeline_doors.py:230`).
  - The template-run door (`dispatcher.py:890-894`) asks about both kinds today, so every
    agentic run would be judged by `ml_engine`. It narrows the same way, from the
    template's operators (`collect_template_operators`, already called there for the
    job-channel door).
  - A launch that reaches no flow kind gets no engine check.
- **Exempt: the `builtin` engine.** An install without the builtin dataflow pack is
  legitimate (`engine_registry.py:69-72`). Its absence keeps surfacing where it does today,
  and the door never refuses for it.
- For each reached kind whose selected engine is not `builtin`, the door reads the engine
  through the one config plane (`selected_engine`) and refuses in these cases:
  - **No installed package provides the engine.** The refusal names the extra
    `cairndev[<engine>]`. This now covers Metaflow as well as SkyPilot: a Metaflow-selected
    environment without the wheel is refused at the door instead of failing at leg time.
  - **The engine offers `check_ready()` and it returns reasons.** An exception from it is
    also a refusal, never a 500, as today.
- `check_ready()` becomes an optional, documented capability of a dataflow engine. It is
  additive, and it is not added to the runtime-checkable `DataflowEngine` protocol, so an
  engine without it still conforms.
- The engine is built for the check with the same factory call the leg will make. The
  builtin, SSH and Metaflow constructors only set attributes (`engines_ssh.py:206`,
  `engines_metaflow.py:73-113`). A test pins that building any registered engine is free of
  side effects.
- The empty-target rule (`skypilot_door.py:92-101`) moves into `SkyPilotEngine.check_ready()`.
  The engine owns what it needs.
- The three call sites keep their position, before any record exists (ADR-0099 §1,
  ADR-0104 D5). The preflight blocker kind stays `engine-not-ready`.
- **The refusal names change:**
  - the pipeline door's refusal name: `skypilot` → `flow-engine`;
  - the dispatcher's `ValueError` prefix: `skypilot door:` → `flow-engine door:`.

  `tests/core/test_pipeline_service_doors.py:160,317` assert the old text and change with
  them. If any API or console surface carries either name, it is a contract change and is
  treated as one.
- **Kept as it is:** the pipeline door still fails open when the door itself raises
  (`pipeline_doors.py:244`); the dispatcher and preflight keep refusing.

*(d) What still names SkyPilot.* The Helm chart's guard (`_helpers.tpl:186-191`) stays: it is
deployment data for an optional engine, not core code.

**D4 — Prefect is removed.**
- **What goes:**
  - `engines_prefect.py`, `prefect_bridge.py`, the `prefect` entry point and extra,
    `test_prefect_conformance.py`;
  - the `prefect` row of `cairn doctor` (`doctor.py:213`);
  - its row in `THIRD_PARTY_NOTICES.md`;
  - the `prefect` member of the platform's `engines` extra.
- **What gets reworded:**
  - the engine lists in help text: `settings.py:976,985`, `backend_selection.py:8`, the
    `cairn/core/dataflow/*` docstrings, `cli/commands/{flow,remote}.py`, and the
    `dataset_readiness` flow docs.
- **What gets refused:** no profile or deploy file selects `prefect`.
  - **Once PR4a lands:** a profile that still does is refused at the launch door by D3's "no
    installed package provides the engine".
  - **Between Prefect's removal and PR4a:** such a profile fails the way any uninstalled
    engine does today, at submit after the record exists.
- **The flow export:** `cairn flow export` keeps `metaflow` and loses `prefect`
  (`cairn/core/dataflow/export.py:54`, `EXPORT_TARGETS`), so no command emits code for an
  engine cairn does not run.

**D5 — A profile ships in `cairndev` only when every binding it makes resolves against the
public set; the rest are the platform's.** *(accepted 2026-10-16)*

The inventory, from main 0185b131 (each profile's own bindings plus what it inherits):

| Profile | Extends | Binding outside the public builtins | Placement |
|---|---|---|---|
| `local-dev` | — | `eval.delta = eval/delta` (`local-dev.toml:42`; official `eval` pack) | **stays**, without that binding |
| `sandbox` | local-dev | none of its own | stays |
| `secure` | local-dev | none of its own (`redact/text`, `audit/store` are builtin) | stays |
| `prod-infisical` | secure | none of its own (`secrets/infisical` is builtin, ADR-0127 D3) | stays |
| `production` | secure | `model/gate` thresholds (official `eval`), `dataset/pii_scan backend = presidio` (official `dataset_checks`) | **platform** |
| `prod` | production | `pack_policies` from `cairn/packs/incident` | **platform** |
| `prod-dbos` | prod | through `prod` | **platform** |
| `e2e` | secure | `s3/put`, `s3/get` (official `s3`), `notify/slack` (official `notify_slack`) | **platform** |
| `campaign` | prod | `s3/*`, Temporal, Metaflow, a GPU target | **platform** (ADR-0128 owner decision 5) |

**What changes:**
- **The platform's profiles get a real directory of their own.** The platform-only profiles
  move to a platform source directory in the monorepo. `deploy/prod/profiles/` stops being a
  symlink to `cairn/profiles` (#2576). The carve's `write_profiles()` merges the public set
  with the platform's into the platform tree, and the prod compose file already mounts that
  directory over the wheel's (`deploy/prod/docker-compose.yml:172,244`).
- **`eval.delta` follows the eval pack.** It leaves `local-dev` and is bound by the platform's
  own profiles: `production`, and a platform dev profile for the platform's local loop.
- **The public deployments point at a public profile.** The Helm chart requires a profile
  (`values.yaml:159`, `profile: ""`). Its three `values-*.example.yaml` files, and the
  fail-message hint at `_helpers.tpl:173`, name `prod.toml`. Those and the light stack
  switch to `secure` (or `prod-infisical`). A public user who deploys the chart gets a profile
  their install can satisfy; the platform's values keep naming `prod`.
- **Code and tests follow the move:**
  - `core/pack/profile.py:213-215` and `core/deploy_check.py:205` stop naming platform files
    in their messages.
  - The public tests that read the moved files become platform tests, or read a public
    profile: `test_production_profile`, `test_deployment_profile_required` (prod, prod-dbos),
    `test_deploy_check` (prod), `test_runtime_from_profile`, `test_secure_profile:191`
    (campaign), `test_profile_curation` (`offered == ["prod", "sandbox"]`), and
    `tests/deploy/test_helm_chart.py:277`.
- **The console's offered list** (`ui_contract.list_profiles`, PlaygroundView) then holds
  only public profiles on a public install. The "Runs in **campaign**" banner
  (`sandbox.toml:4-8`) cannot recur there.

**D6 — Ask's engine wiring leaves `cairndev`; the platform supplies the `ask` command through
the existing `cairn.cli` group.** *(accepted 2026-10-16)*

**What ships today and what it does:**
- `cairn/ask/` (375 lines) has three parts:
  - **the binding:** `__init__.py` binds eight call-time seams into the platform's
    `cairn_ask`;
  - **the aliases:** three 11-line modules, `agent`, `classify` and `resolve`;
  - **the REPL:** `repl.py`, 273 lines.
- `cairn_ask` imports nothing from `cairn`, a rule `tests/ask/test_cairn_ask_boundary.py`
  enforces, so the binding has to live where `cairn.core` may be imported.
- In the public tree none of it runs:
  - `cli/main.py:117` registers `cli/commands/ask.py` only when `cairn_ask` is importable,
    and otherwise mounts a hidden stub that says Ask is part of cairn-platform.
  - `cli/tui/app.py:75,371,471` swallows the import error.

**What changes:**
- **The code moves to the platform.** `cairn/ask/` and `cli/commands/ask.py` move into the
  platform package (`packages/cairn-platform`). That package already registers a provider in
  `cairn.cli` (`packages/cairn-platform/pyproject.toml:46`, ADR-0120 D4).
- **The platform's own importers follow it.** Its routers (`routers/ask.py:33,84`) and
  `console/api.py:333` import from the new home.
- **The public stub yields to a provider.** Today "the public CLI wins every name"
  (`cli/providers.py`), so the stub would block the platform's `ask`. The stub becomes
  "mount the stub only if no provider supplied `ask`". That is a small change to
  `cli/main.py`, not a new group.
- **The TUI asks the same question.** Its three Ask hooks become available only when an
  `ask` provider is mounted, instead of importing a module core no longer ships.
- **No new extension point:** `cairn.cli` exists. `tests/cli/test_cli_root.py:24-41`, which
  covers the stub, stays public.

## Consequences

- `pip install cairndev` installs only what the feature proof covers. Metaflow and SkyPilot
  are one extra away, and their adapters keep their tests in the public CI.
- The first PyPI release carries two more packages. The account-wide token path (#2430)
  covers them.
- Core no longer carries a SkyPilot-specific launch door or install hint. A new engine that
  needs a pre-launch check implements `check_ready()` and gets the door for free.
- Core still names both engines as configuration: the selection keys and console options in
  `profile_backends.py:97-122,204-220` and `settings.py:976,985,1049-1066`. These are data
  read through the one config plane, not imports.
- The platform pins `cairndev[metaflow]` (and `cairndev[skypilot]` if it uses SkyPilot) and
  retargets its image and `engines` extra.
- `cairn-extensions` has no scheduled first move; ADR-0132's dormancy now has no end date.
- A public install offers four profiles (`local-dev`, `sandbox`, `secure`, `prod-infisical`).
  The platform owns its production chain and its eval binding.
- `cairndev` carries no Ask code. The platform's `ask` command arrives through the same
  provider group as its other commands.

## Verification

- `tests/core/test_engine_registry.py`: core never imports an operator pack.
- `tests/core/test_architecture_boundaries.py`: no operator pack → operator pack edge.
- The two new wheels' moved tests pass in the public tree.
- `tests/core/test_skypilot_door.py` becomes the engine-door test. Rejecting cases:
  - an engine not installed;
  - SkyPilot with an empty target;
  - `check_ready` reasons;
  - `check_ready` raising.

  Each must refuse before a record exists. A `builtin`-only environment gets no refusal, and
  instantiating the builtin engine for the check has no side effect.
- Accepting cases for D3c's scope:
  - an agentic-only template with `ml_engine = metaflow` and no Metaflow wheel is admitted;
  - an install with no dataflow pack and `builtin` selected is admitted.
- `tests/core/test_architecture_boundaries.py`: the (C) row for `engines.py` →
  `cairn.core.backend_selection` is gone, and no new row is added.
- `scripts/check_public_set.py`: an optional wheel installed without its extra is refused.
- Every profile left in `cairn/profiles/` compiles against the public builtins alone. This is
  a new guard in the public set, and a profile binding a platform operator must fail it.
  The platform tree's `deploy/prod/profiles/` holds the full set, and its ship guard passes.
- Public install: `cairn ask` prints the stub. With the platform provider installed, `cairn
  ask` is the platform's command. `tests/ask/*` pass in the platform tree, and the
  `cairn_ask` boundary test still holds.
- The public CI job and the hello-cairn feature proof (57/57) stay green. The platform carve
  builds, and `ship_platform.sh`'s guard passes. The leak scan stays at 0.

## Alternatives rejected

- **Move the adapters to `cairn-extensions`** (ADR-0128 D2). A second public repository and
  release train for two packages; ADR-0132 keeps it dormant.
- **One `cairn-operator-dataflow-engines` wheel** (ADR-0128 owner decision 2). That was needed
  only because SkyPilot imported Metaflow's helpers. Promoting the three helpers removes the
  edge, and one wheel would install SkyPilot's adapter for a Metaflow user.
- **Keep the SkyPilot door in core and only move the adapter.** Core would keep naming an
  optional engine and its install hint. The engine-neutral door costs the same, and covers
  Metaflow's "not installed" case too.
- **Keep Prefect as an optional wheel.** Nothing selects it, and nothing proves it.
- **Move only `campaign`** (ADR-0128 owner decision 5). It leaves `prod` and `production` in
  the public wheel. Those bind Presidio, the eval pack's model gate and the incident pack's
  policies, which a public install cannot satisfy. The public Helm chart would keep defaulting
  to a profile its users cannot run.
- **Keep `cairn/ask/` as documented hook code.** It is 375 lines a public user can never run,
  in a wheel that cannot take them back. The `cairn.cli` group already carries the
  platform's commands.
