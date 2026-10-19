# ADR-0123 — What Phase 1 and Phase 2 of the open-source release changed in ADR-0120, ADR-0121 and ADR-0122

**Status:** accepted (2026-10-13, epic #2399; D4 and D6 amended by ADR-0124). Every item below records an owner decision taken
on an issue during the work (#2421, #2425, #2426, #2427, #2428, #2429, #2453, #2455) and already
merged; this ADR exists because the three ADRs it amends are locked and are not edited.

## Context
ADR-0120 (boundary and extension points), ADR-0121 (repositories, layout, names) and ADR-0122
(the `CAIRN_*` prefix) were written before the work and said what should be true. Doing the
work found eleven places where the shipped result differs from their text, each decided by the
owner on the issue that found it. A reader of the three ADRs needs the corrections in one place.

## Decision

**D1 — ADR-0120 D2, the public API.** The public server mounts, unconditionally, `/token`,
`/auth/status`, `/healthz`, `schedules`, `pipelines`, `runs`, `reviews`, `registry`,
`templates`, `start`, `run_control`, `ingress` + the spec-declared trigger routes, `inbox`,
**`accounts`** (`/auth/*`, `/workspaces`, `/variables` — the public CLI logs in and scopes by
workspace; #2427) and **`jobs`** (the pull lane: `cairn worker --pull` claims jobs from the
public server; #2427) and **`oauth`** (`/oauth/callback`, which ADR-0120 D2 listed as platform:
the public CLI's `cairn connect --via oauth` redirects there by default; #2427). The `admin` router of ADR-0120's text is split: `/admin/*` and `/demo/*`
are the platform's. `ask` is the platform's (`/ask` needs the private `cairn-ask` extra);
`CairnClient.ask` is documented platform-only. The server-rendered `/ui` pages are deleted, not
moved (#2426); the `/ui/api/*` routers are the platform's.

**D2 — ADR-0120 D4, the extension-point groups.** Five groups exist, not four: `cairn.routers`,
`cairn.model_catalogs`, `cairn.run_hooks`, `cairn.seeds` and **`cairn.cli`** (#2421 decision 2:
a provider returns a typer sub-app the public `cairn` mounts; the public name always wins).
`cairn.run_hooks` carries seven events, not the three the ADR implied: `run.admitting` (a QUERY
— a provider may raise `AdmissionRefused`, re-raised by the door; everything else fails open),
`run.launched`, `run.finished`, `run.failed`, `leg.finished`, `run.events_ingested` and
`workspace.created`. The `cairn.model_catalogs` provider and the `platform-demo` seed are
registered by the platform package, not the root.

**D3 — ADR-0120 D7, the operator contract.** An operator pack imports `cairn.core.operator`
(plus `cairn.core.llm` / `cairn.core.eval` for those operators) and `cairn.core.dataflow` for
engine and stage providers (#2453 group D). What a pack needs from the core beyond that is
re-exported BY `cairn.core.operator` (`_CONTRACT_REEXPORTS`: connections, the artifact
registry, workspace, redaction/audit/notify types, provider settings, `getenv`) or reaches it
through `OperationContext` (`launch`, `run_store()`, `schedule_store()`, `fingerprint_store()`;
#2455, #2453 group B). The 35 remaining edges are inventoried debt that leaves with the
platform's packs (`PACK_CORE_INVENTORY`, only shrinks).

**D4 — ADR-0120 D3, what stays in core.** Eleven product modules remain in `cairn/core`, each
held by a core site named in `cairn/core/CLAUDE.md` and each waiting for the decision #2497
lists: the licence door stays by decision (#2421 decision 1); the eval chain, the judges store,
`actions/`, dataset roles, knowledge corpora and the incident observability integration wait.

**D5 — ADR-0121 D2, the platform in the monorepo.** The platform lives in
`packages/cairn-platform` (distribution `cairn-platform`, import `cairn_platform`) inside this
repository until #2427 lifts the directory into the private repository unchanged (#2421
decision 3): product modules, the console routers and backends, the product CLI commands, the
hook providers, their tests.

**D6 — ADR-0121 D3, the builtins and the names.** Twelve builtin operator packs, not eleven:
`cairn-operator-remote` (remote/exec over asyncssh) is public, so the builtin dataflow pack's
SSH dispatch imports a public sibling and the `[ssh]` extra is offerable publicly (#2428).
45 shipped manifests carry `publisher = "cairn"` (the ADR's 48 was a miscount). Package URLs
point at `github.com/cairndev-sh/cairn`; image names, the registry repository and issue
references keep `evs-cmd`.

**D7 — ADR-0122 D2 and D4, the fallbacks that outlive the rename.** Until 1.0: the Helm chart
renders both names for the DSN family (`STORAGE`, `CHECKPOINTER_DSN`, `DBOS_DSN`), because a
user's `existingSecret` is the last `envFrom` source and Helm cannot express a fallback; the
control plane sends both `CAIRN_JOB_CHANNEL(_STREAM)` and the `OBS_` twins to the training box,
whose stdlib-only `live.py` prefers the new name (box images are pinned separately);
`OBS_ENGINE_URL` is the legacy alias of `CAIRN_ENGINE_URL` (one setting); the Metaflow
allowlist forwards both prefixes; pydantic's `obs_backend` reads both through `AliasChoices`.
`tests/core/test_env_inventory.py` lists the legacy literals with their reasons and nothing
else may read an `OBS_` name directly.

**D8 — Phase 2's entry gate and order.** Phase 2 started 2026-10-13 before the contract
track's gate (b) (V2 #2243, #2247) was met, in this order: renames (#2429), the public tree
assembled locally with fresh history and not pushed (#2428, `scripts/oss/assemble_public_tree.py`
with its leak scan), the platform carve prerequisite (#2427). The public flip (#2430) stays
behind the gate and the security review.

## Consequences
- The public-core boundary tests encode this ADR: `PUBLIC_SURFACES` (D1), the five groups'
  loaders and `cairn doctor` rows (D2), `PACK_CORE_INVENTORY` and the re-export test (D3),
  `PRODUCT_MODULES` tagged #2497 (D4), the core→platform rule with no allowlist (D5), the
  distribution-name tests (D6), the env inventory (D7).
- ADR-0120, ADR-0121 and ADR-0122 are read together with this ADR; their status is accepted
  as amended here.

## Verification
`tests/core/test_public_core_boundary.py`, `tests/core/test_architecture_boundaries.py`,
`tests/core/test_distribution_names.py`, `tests/core/test_env_inventory.py`,
`tests/core/test_run_hooks.py`, `tests/scripts/test_assemble_public_tree.py`; CircleCI's
`public` job proves the public set installs alone and passes the listed `tests/core` files.
