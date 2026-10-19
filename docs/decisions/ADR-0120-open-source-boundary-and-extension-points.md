# ADR-0120 — The open-source boundary and the core's extension points

**Status:** accepted (2026-10-13, #2400, epic #2399; amended by ADR-0123). Owner decisions of 2026-10-11 are
recorded in D1–D3; D4–D8 need approval. Builds on ADR-0026 (operator packs as wheels),
ADR-0106 (only declared pipelines run) and ADR-0111 (contracts as data-only distributions).
Sequenced after the contract/capability track's first slice (#2242); it changes nothing that
track owns.

## Context
cairn will publish its core under Apache-2.0 and keep the rest private
(`design/oss-release-plan.md`). Today the line between the two exists only by convention:

- `pip install cairn` installs every operator pack, contract and satellite: the root
  `pyproject.toml` hard-depends on all 36 operator packs, `cairn-client`, `cairn-ask`,
  `cairn-sdk` and the 5 contract wheels.
- Domain packs are not distributions: their 9 `cairn.packs` entry points live in the root
  `pyproject.toml` and point into the core wheel.
- Core imports the training pack in two modules (`core/models_catalog.py:100`,
  `core/recommender.py:102`). One sits on the dispatch path: the licence door
  (`dispatch/dispatcher.py:872`) reaches it with no guard.
- Core names operator packs as strings the import test cannot see (`operator/jobs.py:467`,
  `dataflow/export.py:265,378`, `dispatcher.py:1228`, `operator/manifest_sync.py:50-58`,
  `triggers/routers/setup.py:32`).
- One `server.py` mounts 30 routers. The CLI and `cairn-client` use 11; the other 19 exist
  only for the console.
- About 14k lines of product logic live in `cairn/core/*.py` (pilots, recommender, training
  history, judges, the eval ledger, playground, demo scenarios).

`CLAUDE.md` §5 forbids adding an entry-point group or moving a responsibility between
boundaries without an approved decision. This is that decision.

## Decision
**D1 — The public core is API + CLI.** It is the engine (dispatch, engine, operator substrate,
pack system, governance, storage of the record, the in-process runtime, the dataflow contract
and engine selection), the CLI, the HTTP API the CLI and `cairn-client` use, the 11 builtin operator packs, every contract a builtin implements (today `dataflow`,
`audit`, `mcp`, `artifact` and `monitor`; the rule: a contract whose provider ships in the
core ships in the core), `cairn-client`, `cairn-sdk`, the
profiles `sandbox`, `secure` and `local-dev`, `deploy/light` and `deploy/helm`, and one
example pack. There is no console and no console-only API in the public release (owner,
2026-10-11).

**D2 — The public HTTP API is what the CLI and client call.** `/token`, `/healthz`, and the
routers `start`, `pipelines`, `runs`, `run_control`, `reviews`, `templates`, `registry`,
`schedules`, `ingress` and `inbox`. Everything else (`admin`, `setup`, `features`,
`core/console`, the `/ui` web UI and its `/ui/api/*` routers, `builder`, `datasets`,
`capture`, `judges`, `training_runs`, `model_catalog`, `playground`, `insights`, `jobs`,
`ask`, `oauth`, `chat`) moves to the private platform with the console. The public
`openapi.json` is generated from the public app.

**D3 — Everything else is the platform.** Domain packs, the 25 official operator packs, the
contracts no builtin implements (`eval`, `dataset`, `dataset-stages`, `ingest`), the product modules listed in Context, the console, `ask`, and
the owner's operations material. The platform depends on the public wheels. The public core
never imports it.

**D4 — Core reaches the platform only through entry points.** Four new groups, each optional
(absent means the feature is off, never a crash):

| Group | Replaces | Absent means |
|---|---|---|
| `cairn.routers` | console-only routers hard-wired in `server.py` | the server mounts only D2 |
| `cairn.model_catalogs` | `models_catalog` / `recommender` importing `cairn.packs.training` | the licence door has no catalogue; a `base_model` input is not checked against one |
| `cairn.run_hooks` | the dispatcher calling the eval ledger, judges and continuous eval by name (`dispatcher.py:1228`) | a finished run is recorded and scored by its own declared evals only |
| `cairn.seeds` | `cairn demo` seeding `training/*` and `incident/*` | `cairn demo` seeds the example pack |

Each group's loader lives beside today's loaders (`pack/loader.py`, `operator/loader.py`),
fails open with a logged warning like them, and is listed in `cairn doctor`.

**D5 — Starter content and operator names come from metadata.** Starter packs and setup goals
are pack metadata (`starter: true`, `goals:` in the pack manifest), not `STARTER_PACKS`. The
remote-job entry point is the operator manifest's (`entry =`), not `operator_training`. The
code `dataflow/export.py` generates imports a module named by the engine's entry-point
metadata. The reports backfill list is read from manifests that declare a report.

**D6 — The trigger grammar is not HTTP.** `cairn/core/triggers/declared.py` moves to
`cairn/core/engine/triggers_grammar.py`. The schedule store, the inbox and token minting get
homes below the surface layer. No module below the surface layer imports
`cairn.core.triggers` (today 20 places in 15 files do). The CLI is a surface and may
(it uses the auth helpers in two places).

**D7 — Operators import only the operator contract.** An operator pack imports
`cairn.core.operator` (and `cairn.core.llm` / `cairn.core.eval` for LLM and eval operators, as
today), never dispatch, engine internals or triggers. An operator that must start a pipeline
(`monitor/dispatch`) does so through an optional `OperationContext.launch` handle the dispatch
layer injects, so admission, gates and budget still apply; absent the handle, it reports
`dispatched=False` (owner, 2026-10-12, 2455). `ingest/watch` uses a public schedule API.

**D8 — The boundary is a test.** A CI job installs only the public set into a fresh
environment and runs `tests/core` and the architecture tests. Static rules forbid
`cairn.packs`, `operator_*` (import or string) and lower→`cairn.core.triggers` edges in core,
and a console-only router in the public server.

## Consequences
- Every builtin operator carries `implements` before the core goes public: seven do not yet
  (`artifact_local`, `data_profile`, `memory_local`, `rag_local`, `rag_rerank`, `redact_regex`,
  `secrets_env`). That is the contract track's V4 (#2245, enforced by #2247) and a gate for
  Phase 2 (#2430); a builtin nobody uses is retired there rather than migrated.
- The two known core→packs violations and the string edges are removed rather than
  allowlisted.
- `pip install cairndev` (ADR-0121) installs only the public set. Platform installs add their
  wheels.
- Tests that need a domain pack (145 of 494 `tests/core` files) move with their pack, or are
  rewritten against the example pack.
- Behaviour without the platform: no model catalogue, no judges or pilots, no console. The
  record, gates, budgets, policies and replay are unchanged.
- Four entry-point groups are added; `cairn.trigger_providers`, which has no producer, is
  removed (#2408).

## Not decided here
Repository layout and package names (ADR-0121). The environment prefix (ADR-0122). Anything
the contract/capability track owns: the `cairn.resolution/1` record, the `implements`
grammar, `[profile.bindings]`, the workflow registry tables.

## Verification
#2424: the public wheel set installs alone and passes `tests/core`. #2414, #2415, #2416,
#2417, #2421 and #2422 each land with the static rule that pins them.
