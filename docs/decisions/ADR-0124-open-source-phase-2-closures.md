# ADR-0124 — Phase 2 closes the product-module inventory, names the public images and README, and keeps the process material private

**Status:** accepted (2026-10-13, epic #2399). Amends ADR-0123 D4 and D6 and clarifies
ADR-0121 D4; those ADRs are locked and are not edited. Every item records an owner decision
taken on #2497, #2424, #2427 or #2428 and merged (#2516, #2517 and the #2428 follow-up).

## Context

ADR-0123 D4 left eleven product modules in `cairn/core` "each waiting for the decision #2497
lists", and its Consequences bound the boundary tests to a `PRODUCT_MODULES` inventory. D6 said
image names keep `evs-cmd`. The standalone rehearsal of the public tree (#2428) and the carve
rehearsal (#2427) then surfaced what the assembled tree still carried of the platform: the
monorepo README with private extras, the prod tooling's names in comments, docs, chart and
runbooks, the agent workflow document, and a leak scan that could not tell an issue reference
from a private link.

## Decision

**D1 — ADR-0123 D4, the inventory is closed (#2497).** `actions/` moved to
`cairn_platform.actions`: the runner keeps the `ActionHandler` protocol and the platform's
`actions` provider in the existing `cairn.routers` group injects the handler through
`RouterDeps.runner`. `cairn/integrations/observability` moved to
`cairn_platform.integrations.observability`, re-pointing the incident pack and the four official
packs that import it. The licence door, the eval chain with the eval ledger, the judges store
with judge agreement and labels, dataset roles with the eval cutoff, and knowledge corpora are
core **by decision**, each justified in `cairn/core/CLAUDE.md`. `PRODUCT_MODULES`, its strict
xfail and the inventory tests are deleted; a green test that measures nothing is not kept.

**D2 — ADR-0123 D6, the public image names.** Public images publish at
`ghcr.io/cairndev-sh/cairn-engine` and the chart at `ghcr.io/cairndev-sh/charts`; the
assembler rewrites the registry in the chart and `Dockerfile.engine`. The platform's own
images, the registry repository behind them and issue references keep `evs-cmd`.

**D3 — ADR-0121 D4, what process material means.** The agent files (`.claude/`, `.codex/`,
every `CLAUDE.md` and `AGENTS.md`), `docs/architecture/17-issue-workflow.md`, the training
architecture (`docs/architecture/training/`, which documents the private training pack) and the
runbook for the platform's ship script are the platform's. `CONTRIBUTING.md` carries the public
process. Revisited in Phase 3 at the earliest.

**D4 — the public README.** The public tree's README is the landing page `cairndev-sh/cairn`
already publishes, kept with its logo under `scripts/oss/public/`; the assembler generates
its Quickstart from the commands the public CI job proves. The monorepo README stays the
platform's.

**D5 — the prod tooling.** `cairn deploy`, `deploy_check`, `compute_lifecycle` and the
pipeline grammar ship as code. Comments, docs, chart, runbook and `pyproject.toml` lines that
name `deploy/prod`, the ship or box scripts or a private distribution are rewritten or
dropped by the assembler's scrub rules, each of which must match or the assembly fails.
ADRs are dated records carried as written, and the leak scan exempts them; an issue or pull
request reference to `evs-cmd` is allowed by ADR-0123 D6 and the scan no longer reports it.

## Consequences

- `tests/core/test_public_core_boundary.py` carries no product-module inventory; the
  core→platform rule (ADR-0123 D5) is the one that holds.
- `docs/decisions/README.md` marks ADR-0123 as amended by this ADR.
- The assembly report's leak scan reads zero on 2026-10-13; #2430 reviews the assembled tree,
  not the scan, before the flip.

## Verification

`tests/scripts/test_assemble_public_tree.py`: the README, the private doc paths, the scrub
(with the rejecting case of a rule that stops matching) and the scan's allowances (with the
rejecting cases of a private link, a prod path outside an ADR and a private pack name).
`tests/core/test_public_core_boundary.py` and `tests/core/test_architecture_boundaries.py`
for D1.
