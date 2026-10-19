# ADR-0085: Team-scale DX — console seam via OpenAPI contract, top-level `console/`, one-command sqlite dev env

- **Status:** Proposed
- **Date:** 2026-08-03
- **Refines:** epic #1088 (strategy note, 2026-07-22) into decisions
- **Related:** ADR-0030 (kind-first source layout), ADR-0083/#1090 (re-architecture
  — this ADR deliberately touches NONE of its surfaces), #408 (OSS-core vs EE —
  the only repo-split trigger)

## Context

The repo is optimized for one developer with full context. Onboarding anyone —
especially a frontend engineer — currently requires absorbing tribal knowledge
that exists only in session memory: the `.env` postgres DSN that breaks local
runs, `OBS_STORAGE` unset meaning in-memory, the uv-sync extras wipe, the vite
`API_PATHS` hand-list where every new route silently 404s in dev, and a
`cairn/core/triggers/server.py` that has reached **3,042 lines** and collides on
every parallel backend PR.

Facts checked against the code (2026-08-03):

- `cairn/web/package.json` already has `gen:api` = `openapi-typescript` v7 —
  but it pulls from a **running engine's** `/openapi.json`, so type generation
  itself requires a working Python environment. The seam exists; it points the
  wrong way.
- `cairn/web/vite.config.ts` maintains a hand-written 20-prefix `API_PATHS`
  list — the recurring silent-404 trap.
- No `cairn dev` command exists; the console dev loop is engine-start folklore.
- The console `dist` ships inside the wheel (`uv pip install` bakes it);
  backend and console version together today.

Two of these choices are one-way-ish doors (repo layout; what artifact is the
contract), which is why #1088 required an ADR before build.

One more fact, checked 2026-08-03 because it kept being assumed wrong: **the
CLI and the ask agent do NOT sit on the API.** 25 of 28 CLI command modules
import `cairn.core` directly and run in-process (only `remote.py` and part of
`doctor.py` speak HTTP); all 10 `cairn/ask` modules import core directly. The
CLI is the engine embedded in a terminal. Every large OSS control plane
enforces the opposite: kubectl may only use the public API; docker/cli depends
only on the API client package; Vault's CLI wraps its separate `api/` module;
Temporal's CLI meets the server at gRPC; Prefect's CLI is a pure API client
with an ephemeral local server for no-server use. Airflow is the cautionary
tale — its CLI/workers imported internals + the DB directly for years, and the
multi-contributor pain eventually forced AIP-44/Task SDK ("everything through
an API"). Monorepo vs multi-repo is NOT the variable; the enforced client
boundary is.

## Decision

### D1 — The boundary is the OpenAPI contract, committed as an artifact

A generated **`console/openapi.json` is committed** and becomes the single
source of truth for the console↔engine seam.

- Generation is **offline**: a script builds the FastAPI app in-process and
  dumps `app.openapi()` — no running server, no database, no keys. (This is
  what makes zero-Python FE work possible; today's live-engine `gen:api` is the
  inverted version of this.)
- CI checks the committed spec is **fresh** (regen + diff = fail if stale) and
  runs **oasdiff** against the base branch: a breaking change fails the PR
  unless labeled `api-break` (which forces a console-side change in the same
  PR or a linked one). Backend PRs can no longer break the console silently.
- `npm run gen:api` re-points at the committed file. Types stay
  `openapi-typescript` (already adopted, types-only, zero runtime); we do NOT
  adopt a full client generator (orval/hey-api) — the console already has a
  fetch layer, and codegen'd clients would churn every screen for no capability
  gain. Revisit only if the hand-written fetch layer itself becomes the
  collision point.

### D2 — The vite proxy is derived, never hand-maintained

`API_PATHS` is replaced by a build-time derivation from `console/openapi.json`
(top-level path prefixes, plus the static `/console` mount). A new backend
route becomes visible to the dev proxy the moment the spec regenerates. The
hand-list and its trap are deleted.

### D3 — `cairn/web` is promoted to top-level `console/` (monorepo, hard seams; NOT a repo split)

- `console/` gets its own README, CI job (tsc + eslint + prettier + vitest +
  build), lint config, and CODEOWNERS line. An FE dev works without opening a
  `.py` file.
- **Versioning is unchanged:** the wheel build copies `console/dist` in;
  backend and console still release together. The only repo-split triggers
  remain licensing divergence (#408 OSS-core vs EE) or a genuinely independent
  release cadence — a split is a NEW ADR, not an amendment to this one.
- The move is mechanical (`git mv` + path fixes in Dockerfile.engine, wheel
  packaging, deploy recipes, CI). Deploy gotcha #17 (dist baked into the wheel,
  gitignored, rsync'd explicitly) carries over with the new path.

### D4 — `server.py` splits into FastAPI routers by surface

`cairn/core/triggers/server.py` (3,042 lines) becomes a thin app-assembly
module plus `APIRouter` modules per surface (runs, pipelines/flows, packs +
registry, triggers + schedules, auth/admin/users, console/ui, insights/ask/
playground). Route paths, auth behavior, and the OpenAPI output stay
byte-identical — D1's spec-freshness check is the regression gate for the
split itself. No handler logic changes ride along.

### D5 — `cairn dev`: one command, sqlite, keyless, seedable

A `cairn dev` CLI command that:

- pins `OBS_STORAGE=sqlite://~/.cairn/dev.db`, `OBS_RUNTIME=inprocess`, and
  **explicitly overrides the repo `.env`** (the stale postgres DSN foot-gun is
  retired by construction, not documentation);
- seeds auth with `dev/dev` (auth cannot be silently off — the console's login
  flow is part of what FE devs work on);
- starts engine + vite together (`--engine-only` / `--web-only` flags);
- `cairn dev --seed` loads deterministic demo data through the existing
  mock-operator machinery (`supports_mock`/`sandbox_safe`): completed runs,
  lineage chains, a finished clean flow, a gated promotion awaiting approval —
  every console surface non-empty on first paint, zero keys, zero spend;
- binds LLM calls to the mock path unless a real key is present (keyless by
  default).

MSW/Prism mock-server and Storybook (epic plank 2) are follow-ups **enabled**
by D1's committed spec, not part of this ADR's build.

### D6 — Sequencing (locked)

1. **CI restore first, non-negotiable** — PR-gated `tests/core` + pack tests +
   tsc + eslint/prettier + `uv run cairn lock --check`, then D1's spec checks.
2. `cairn dev` + seed (D5) — the CONTRIBUTING quickstart depends on it.
3. Contract + proxy derivation (D1, D2).
4. `console/` promotion + router split (D3, D4) — **in a different month than
   1–3**; never restructure the repo and its tooling in the same window.
5. Branch protection, CODEOWNERS, CONTRIBUTING.md (15-min quickstart IS
   `cairn dev`), pre-commit, ~10 curated good-first-issues.
6. `cairn-client` + CLI/ask porting (D7) — after the committed spec exists;
   directory promotion of `cli/`/`ask/`/`client/` only after the porting.

### D7 — `cairn-client`: one Python API client; CLI and ask consume it

- A **`client/` workspace package (`cairn-client`)** is generated from the
  committed `openapi.json` (D1) plus a thin hand-written wrapper (auth/token,
  retries, SSE). The operator packs already prove the uv-workspace
  multi-distribution shape — this is one more member, not new machinery.
- **The CLI splits by persona, not wholesale** (the kubectl/kubebuilder line):
  - *Platform-user* commands (runs, pipelines, gate/approve, series, lineage,
    connect, pilot, registry browse…) are ported to pure `cairn-client` calls.
  - *Author/dev* commands (`lock`, `validate`, `init`, `pack build`, `test`,
    `visualize`) legitimately operate on the source tree and STAY
    core-importing dev-tools.
- **Local-first is solved the Prefect way, not the Airflow way**: with no
  server configured, user commands start/attach to an ephemeral local API
  (`cairn dev`'s engine, D5) and talk to localhost — ONE code path, never a
  second in-process branch per command.
- **`ask/` becomes a `cairn-ask` package** whose tools call `cairn-client`;
  it stays servable inside the engine process (the `/ask` endpoint) — the
  package boundary is about imports, not about where it runs.
- **CI enforces the boundary**: an import-linter rule that ported `cli/`
  user commands and `ask/` may not import `cairn.core` — same pattern as the
  existing test asserting `leg_executor` imports no langgraph.
- Directory promotion to top-level `cli/`, `ask/`, `client/` happens ONLY
  after the client exists and commands are ported — a directory move before
  the contract exists is cosmetic (see Rejected).

## Rejected

- **Repo split now** — doubles CI/release surface with no consumer; dist ships
  in the wheel and versions together. Trigger unchanged (#408 or cadence).
- **Full TS client codegen** (orval/hey-api/openapi-generator) — capability
  already covered by openapi-typescript types + the existing fetch layer;
  codegen churn would touch every screen.
- **Live-engine spec as the contract** — a contract you need a running
  backend to read is not a seam; it is the current failure mode.
- **Auth-off dev mode** — hides the login surface FE devs must build against;
  seeded `dev/dev` costs nothing.
- **Docker-compose as the primary dev env** — stays as the no-Python fallback
  (devcontainer), but the primary loop must be sub-second reload, which
  compose is not.
- **Moving `cli/`/`ask/` top-level BEFORE the client boundary exists** — a
  directory move without the dependency contract is cosmetic: both would still
  import `cairn.core` deeply, so nothing becomes independently maintainable.
  Contract first, port second, move third (D7).
- **Wholesale CLI-over-API (Airflow pre-AIP-44 inversion)** — author/dev
  commands (`lock`, `validate`, `pack build`) operate on the SOURCE TREE, not
  the control plane; forcing them through HTTP adds a server dependency to
  pure authoring. The persona split keeps each side honest.

## Consequences

- Backend PRs that change routes carry a visible spec diff; breaking ones fail
  CI. The 422-in-the-browser class of bug moves to PR time.
- The vite proxy trap, the `.env` DSN trap, and the empty-first-paint problem
  are retired structurally.
- Deploy recipes, Dockerfile.engine, and wheel packaging must update paths at
  D3 time — one mechanical PR, verified by the existing console-hash deploy
  check.
- `server.py` split (D4) is high-merge-value but must stay logic-free; the
  spec-freshness check plus full `tests/core` is its gate.

## Execution breakdown (sub-issues to file under #1088)

| # | Slice | Decision |
|---|---|---|
| 1 | CI restore: PR-gated core tests + tsc + lint + lock --check | D6.1 |
| 2 | Offline OpenAPI dump script + committed spec + freshness check | D1 |
| 3 | oasdiff breaking-change gate + `api-break` label flow | D1 |
| 4 | Derive vite proxy from spec; delete `API_PATHS` | D2 |
| 5 | `cairn dev` (env pinning, dev/dev auth, engine+vite) | D5 |
| 6 | `cairn dev --seed` deterministic demo data | D5 |
| 7 | `console/` promotion (git mv + packaging/deploy/CI paths) | D3 |
| 8 | `server.py` → routers (logic-free) | D4 |
| 9 | CODEOWNERS + branch protection + CONTRIBUTING + pre-commit | D6.5 |
| 10 | Curate good-first-issues | D6.5 |
| 11 | `cairn-client` workspace package (generated from spec + wrapper) + import-linter rule | D7 |
| 12 | Port platform-user CLI commands to cairn-client + ephemeral-local-API attach | D7 |
| 13 | Port ask agent tools to cairn-client (`cairn-ask` package) | D7 |
