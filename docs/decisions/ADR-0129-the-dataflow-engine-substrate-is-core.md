# ADR-0129 — The dataflow engine substrate is core

- **Status:** PROPOSED (boundary move approved by the owner 2026-10-15, "continue with 2583";
  the record awaits acceptance with the PR)
- **Date:** 2026-10-15
- **Issue:** #2583 (follow-up to ADR-0127 D5 errata; unblocks the extensions ADR #2535)
- **Amends** ADR-0118 D7's *placement* of the kernel runner (the obligation itself is
  unchanged). Keeps ADR-0081 D3/D4/D5, ADR-0084 D1, ADR-0118 D1–D6/D8 and ADR-0127 D5 unchanged.

## Context

ADR-0118 D5/D7 put the stage-contract check in the kernel runner (`run_dataflow`) and obliged
every engine adapter to run kernels through it. The runner, the stage registry it resolves
kernels from, the job-workdir file readers (`metrics.json` / `result.json`) and the SSH
fetch contract (`fetch_list` / `fetch_dir` / `rewrite_fetched`) all lived in the **builtin
dataflow pack** (`operator_dataflow.engine.runner`, `.stages`, `.engines`, `.engines_ssh`).

ADR-0127 D5 sends the `prefect`, `metaflow` and `skypilot` adapters out of the public tree
to `cairn-extensions`. That move was blocked (ADR-0127 errata, 2026-10-15): an adapter in any
other pack would import the builtin pack's internals, which is an operator-pack →
operator-pack edge, forbidden by `tests/core/test_architecture_boundaries.py` (empty
allowlist; rejecting case for official → builtin). The substrate the adapters need is
engine-neutral by construction (ADR-0081 D3: "stdlib-only; executes stages via the shared
registry — no stage-specific logic may live here"), and `cairn.core.dataflow` already owns
the contract the runner implements (`DataflowSpec` / `DataflowResult`), the conformance suite
that defines an engine, the one validation function (ADR-0118 D5) and engine *selection*
(`engine_registry`, #1512). Only engine *construction* was deliberately left in the pack
(#1512: each factory reads its own configuration).

## Decision

**D1 — the substrate is `cairn.core.dataflow`.** Four modules move from the builtin dataflow
pack into core, behaviour byte-identical:

| Core module | What | Was |
|---|---|---|
| `cairn/core/dataflow/runner.py` | `run_dataflow`, `BudgetExceeded`; the stage-contract checks (ADR-0118 D5), budget enforcement, `.done` markers, checkpoints, map fan-out, descriptors | `operator_dataflow/engine/runner.py` |
| `cairn/core/dataflow/stage_loader.py` | `load_stages`, `stage_versions`, `stage_load_failures`, the conformance stages `noop` / `noop.slow` | `operator_dataflow/stages.py` |
| `cairn/core/dataflow/results.py` | `validate` (registry capability discovery), `read_metrics`, `read_result`, `read_error` — the workdir file contract | `BuiltinLocalEngine.validate/_read_*` in `operator_dataflow/engines.py` |
| `cairn/core/dataflow/fetch.py` | `fetch_list`, `fetch_dir`, `rewrite_fetched`, `BOOKKEEPING_OUTPUTS` — the remote result contract | `operator_dataflow/engines_ssh.py` |

Core reads the `cairn.dataflow_stages` / `cairn.flow_stages` entry-point groups (as
`engine_registry` reads `cairn.dataflow_engines`); it imports no pack, and the AST rule
`test_engine_registry.py::test_core_never_imports_an_operator_pack` and the string rule in
`test_public_core_boundary.py` hold over the new modules. `fetch.py` imports no SSH library:
the transport stays the adapter's; core names only what crosses it. The runner's logger is
`cairn.core.dataflow.runner` (`getLogger(__name__)`; it was named after the pack module).

**D2 — ADR-0118 D7, amended placement.** The obligation stands: an engine that runs kernels
through `run_dataflow` declares `carries_stage_contracts`, and the control plane refuses an
opted-in spec on any other engine. What changes is *where* `run_dataflow` lives: an adapter in
any pack (builtin, official, extensions) imports it from `cairn.core.dataflow.runner`, so the
stage-contract check has one implementation that no pack owns, and ADR-0127 D5's adapter move
no longer needs a pack-to-pack import.

**D3 — construction and the engines stay in the pack.** `BuiltinLocalEngine`,
`BuiltinSshEngine`, the three adapters, their bridges, the `cairn-dataflow-run` console script
(`operator_dataflow.engine.entry`), the `cairn.dataflow_engines` factories and the
`cairn.dataflow_stages` entry points for `noop` / `noop.slow` are unchanged and remain the
builtin dataflow pack's (#1512's split: core selects, packs construct). Moving the adapters is
the extensions step after #2535, not this change.

**D4 — the pack re-exports for one release.** `operator_dataflow.engine.runner`,
`operator_dataflow.stages`, `operator_dataflow.engines_ssh` (the fetch names) and
`BuiltinLocalEngine._read_metrics/_read_result/_read_error` resolve to the core objects
(`tests/core/test_dataflow_substrate.py` holds them identical). No deprecation warning is
emitted — a worker runs thousands of legs and must not log once per import. The shims are
removed in the release after the one that ships this ADR; the pack's own code, the bridges and
the adapters import from core already. Shim users to retarget before removal: the build-time
registry check in `Dockerfile.dataflow-task` (`from operator_dataflow.stages import load_stages`);
in-tree Python imports none (`operator_dataset_checks`' kernel test was retargeted in #2583).

**D5 — the task image needs no new install.** `Dockerfile.dataflow-task` and the train-gpu
image install `cairndev` (core) plus the pack; the substrate's imports (`cairn.core.dataflow.*`,
`cairn.core.operator.batch` for the map pool) were already core, so `cairn-dataflow-run` runs
there unchanged. The image's build-time registry check (`from operator_dataflow.stages import
load_stages`) resolves through the D4 shim until it is retargeted with the shim's removal.

## Verification

- `tests/core/test_dataflow_substrate.py`: the four modules import in a fresh interpreter with
  no `operator_*` module loaded; the pack's re-exports are the core objects.
- `tests/core/test_engine_registry.py::test_core_never_imports_an_operator_pack`,
  `test_public_core_boundary.py` (no operator-module string in core),
  `test_architecture_boundaries.py` (pack → `cairn.core.dataflow.*` is an allowed edge;
  `PACK_CORE_INVENTORY` unchanged), `test_leg_executor.py::test_a_data_leg_never_imports_langgraph`.
- The builtin dataflow pack's own suite — the builtin conformance suite (ADR-0118 D7
  interpreter cases included), the runner's checkpoint / descriptor / kernel-version /
  stage-contract / budget / fan-out tests, the SSH fetch and localize seams, and the prefect,
  metaflow and skypilot adapter tests — runs against the core modules (the tests patch the core
  module now, since a re-export cannot be patched).

## Consequences

- An engine adapter can live in any pack. The ADR-0127 D5 move becomes a directory move plus
  entry-point re-registration (#2535).
- `cairn.core.dataflow` gains four modules and owns the runner; `cairn/core/dataflow/CLAUDE.md`
  records the boundary. The pack's `DESIGN.md` says what moved.
- `cairn.lock`: the builtin dataflow pack's content hash changes (its files changed); no entry
  point, manifest or contract changes.
- Docs that cited `operator_dataflow/engine/runner.py` now cite `cairn/core/dataflow/runner.py`
  (05-operators, the execution / artifacts / operators-and-packs contracts, the training golden
  path).

## Alternatives rejected

- **Allowlist the official → builtin import for the three adapters.** Expands a debt allowlist
  to make a move pass, and leaves every future adapter with the same coupling; rejected by the
  local enforcement rules (no allowlist expansion solely to pass).
- **A `cairn-dataflow-substrate` wheel between core and the packs.** A new distribution for
  four stdlib-only modules whose contract core already owns; ADR-0081 D2 placed that contract
  in the core wheel on purpose so both sides import one thing.
- **Move the engines and factories too.** Drags engine-specific configuration (eight
  `metaflow_*` keys, the SkyPilot knobs) into core — the exact reason #1512 kept construction
  in the pack.
