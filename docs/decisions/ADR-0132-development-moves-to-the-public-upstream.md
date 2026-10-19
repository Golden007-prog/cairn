# ADR-0132 — Development moves to the public upstream: the multi-repo model after the flip

- **Status:** ACCEPTED 2026-10-16 by the owner, with the recommendation of each of the seven
  decisions at the end taken as written.
- **Date:** 2026-10-16
- **Issues:** #2637 (the three-repo development setup), #2430 (the flip), #2399 (the epic).
- **Builds on** ADR-0121 D1/D2 (two repositories; the platform pins the public wheels) and
  ADR-0128 (the extensions repository). **Amends** ADR-0121 D7 (validation on CircleCI → the
  public repository's own GitHub Actions, #2664) and the 2026-10-14 owner decision that
  development stays in the monorepo (it does — until the flip, which this record makes the
  switch point). **Rejects** a synced mirror (Copybara/josh) as the public repository's model.

## Context

1. Today every line is written in the private monorepo `evs-cmd/cairn`. The public tree is
   *generated* from it (`scripts/oss/assemble_public_tree.py`) and the platform repository is
   *carved* from it with rewritten history (`scripts/oss/carve_platform.py`), force-pushed on
   every regeneration. Neither derived repository can take a commit of its own.
2. The owner decided on 2026-10-16 that the public repository will be developed by multiple
   outside contributors. A contributor's pull request must merge where it was opened, and its
   CI must run there. A generated mirror cannot do either: the next regeneration overwrites
   it, and every outside change would have to be carried into the monorepo by a tool or by hand.
3. The dependency direction is already one way and test-enforced: the platform imports the
   core; the core never imports a pack or the platform (root `CLAUDE.md` §3,
   `tests/core/test_architecture_boundaries.py`). A repository boundary can hold what a test
   holds today.
4. The cost of the generated model showed this week: the public tree shipped a stale
   `uv.lock` and install instructions naming extras the public package does not declare
   (#2666); each regeneration takes about an hour and a force-push; the private repositories'
   CI produces no results. A repository with its own CI on every pull request catches such
   drift at once.
5. The working setup was proven on 2026-10-16: three sibling checkouts and one editable venv
   (`dev-install.sh`: `uv pip install --no-sources -e` over every workspace member of the three
   repositories) — 69 packages, every import resolving to its sibling checkout, core and
   platform tests running from the shared venv.

## Decision

**D1 — The public repository is the upstream for the core.** From the flip (#2430) on,
`cairndev-sh/cairn` is where the engine, the builtin operator packs, the core contracts, the
clients, the example pack, the light stack, the chart and the user docs are developed. It is
not a mirror of anything. `evs-cmd/cairn-platform` is a downstream consumer that depends on
released core wheels (ADR-0121 D1) and holds everything private. `cairndev-sh/cairn-extensions`
stays as ADR-0128 defines it and stays dormant until the engine adapters move there; until then
two repositories are active.

**D2 — The monorepo freezes at the flip.** The last carve and the last assembly run from
`evs-cmd/cairn` `main` immediately before the flip. After that the monorepo is read-only
history: never deleted, renamed or rewritten (owner rule), no new commits, its CI off, its
open issues dispositioned per D6. `scripts/oss/carve_platform.py`,
`scripts/oss/assemble_public_tree.py` and the regeneration runbooks retire with it (kept in
the frozen history; removed from the two live repositories in their first commits).

**D3 — How a developer works: one folder, one editable venv.** The maintainers' checkout is a
plain folder holding the active repositories side by side, with one venv installed by
`dev-install.sh` (every workspace member of every sibling, editable, `--no-sources`), so an
edit in the core is live in the platform. Each clone keeps its own git and remote. Tests run
inside the repository they belong to, from the shared venv. `dev-install.sh` and the shared
process files (D7) live in a small `cairndev-sh/cairn-dev` repository (private or public, owner
decision 4).

**D4 — How the platform pins the core, in two modes that never mix.**
- *Dev:* the editable venv of D3; the platform's own `pyproject.toml` is bypassed by
  `--no-sources`.
- *CI and prod:* `cairn-platform/pyproject.toml` depends on a released `cairndev==X.Y.Z` (and
  the builtin wheels at the same version) from PyPI, locked in its `uv.lock`; `public-wheels/`
  is the pre-PyPI stand-in and is removed once the first release is on PyPI. Renovate opens the
  bump PR when a new core version is published.
- *Early warning:* the platform's CI also runs its suite against the core built from
  `cairndev-sh/cairn` `main` (the "nightly against public main" of ADR-0121 D1, now a job on
  every platform PR plus a nightly schedule), so a core change that breaks the platform is
  seen before the release that would carry it.

**D5 — Releases are frequent, lockstep and automated.** A core fix the platform needs reaches
it only through a core release, so patch releases are routine (expected near-weekly, more
often when needed): `uv version --bump patch` across the workspace, a `v*` tag, the release
workflow publishes all wheels by trusted publishing, the image and the chart (ADR-0121 D7).
Every distribution in the public workspace carries the same version. Breaking changes follow
SemVer minor until 1.0 and are announced in `CHANGELOG.md`.

**D6 — Issues and pull requests live where the code lives.** Public-facing issues on
`cairndev-sh/cairn`, platform issues on `evs-cmd/cairn-platform`. GitHub cannot transfer issues
across organisations, so at the flip the open public-facing issues of `evs-cmd/cairn` are
recreated on the public repository by script (title, body, labels, a back-link), the rest moved
to the platform repository the same way, and the monorepo's issue tracker closed. A change that
spans the core and the platform is one pull request per repository, the core's merged and
released first, the platform's referencing the core version it needs; no tool coordinates
them — the convention and the D4 early-warning job do.

**D7 — Process material has one home.** The agent and maintainer process files (the
`CLAUDE.md` constitution and directory rules, `.claude/skills`, the issue workflow, the
memory conventions) are developed in `cairn-dev` (D3) and vendored into each repository where
the tooling expects them, with a check that the copies match. The public repository carries the
scrubbed versions the assembler produces today (ADR-0124 D3), as its own files from the flip on.

**D8 — The public repository is contributor-ready at the flip.** Branch protection on `main`
with required checks (`ci.yml` `public` and `public-e2e`, DCO); a `v*` tag protection rule (an
armed release workflow publishes on any `v*` tag); `CODEOWNERS`; issue and pull request
templates; `good first issue` and `help wanted` labels; a stated review turnaround in
`GOVERNANCE.md`; the files that already ship (CONTRIBUTING with DCO, SECURITY,
CODE_OF_CONDUCT, GOVERNANCE, SUPPORT). Validation runs on the repository's own GitHub Actions
(`ci.yml`, #2664), which amends ADR-0121 D7's CircleCI choice; the CircleCI files are removed
from the public repository in its first commit after the flip.

**D9 — The cutover is one sequence, in this order.** (1) Land or pause the work in flight in
the monorepo. (2) The last proof run, the public tree's own CI job run on the assembled tree,
the last carve and assembly, both pushed. (3) Import the assembled tree into `cairndev-sh/cairn`
with fresh history (ADR-0121 D1), set D8's protection, watch the first Actions run go green.
(4) Regenerate with `--publish`, tag `v0.1.0`, publish; switch PyPI to per-project trusted
publishers. (5) Freeze the monorepo (D2), recreate the issues (D6), stand up `cairn-dev`
(D3, D7), update the platform's pin to the released version (D4), re-ship prod from the
platform repository. (6) Announce. Until step (5) completes, development continues in the
monorepo as decided on 2026-10-14.

## Consequences

- Outside contributors get a real repository: their pull requests merge where opened, under
  the repository's own CI, with no private dependency.
- A core fix reaches the platform only after a release. The maintainers accept the cadence of
  D5 and the two-PR protocol of D6 for cross-cutting changes, which this week were rare (the
  fixes were core-only).
- The carve, the assembler and the regeneration runbooks retire; the time and the force-pushes
  go with them. The assembler's scrub and leak rules become one-off: they run once more at the
  flip, and the leak scan's rules move into the public repository's CI as a check on its own
  tree.
- The platform's CI must work: the `evs-cmd` organisation's Actions billing failure
  (every check fails in 0 s) has to be fixed before step (5), or the platform has no CI.
- Memory, skills and the root `CLAUDE.md` describe a monorepo; they are rewritten for the
  folder-of-repositories model under D7 as part of step (5).

## Verification

- `dev-install.sh` on fresh clones installs every member editable; `python -c "import
  cairn.core, cairn_platform"` resolves each to its sibling checkout; the core's and the
  platform's targeted suites pass from the shared venv (the 2026-10-16 trial: 69 packages,
  `tests/core/test_bus.py` 19 passed, platform tests 9 passed).
- The platform's full suite passes in the shared venv (not yet run — step (5) gate).
- The public repository: first `ci.yml` run green on the imported tree; a probe PR from a
  non-member account merges only after DCO and the required checks.
- The platform's CI: the pinned-core job and the core-`main` job both run; a deliberate
  breaking change on core `main` turns the core-`main` job red without touching the pinned job.
- A core patch release end to end: bump, tag, 31 wheels on PyPI, Renovate's bump PR on the
  platform, the platform's lock updated and green.

## Alternatives rejected

- **A monorepo with a synced public mirror (Copybara, josh, an incremental carve).** Keeps one
  source of truth and one PR per change, and all current tooling. Rejected because the public
  repository is then not where contributors' changes land; every inbound change needs the sync
  tool, and the mirror's history is bot commits. The owner expects multiple contributors
  (Context 2).
- **A single public monorepo with the platform as the only private repository (extensions as
  a directory).** Fewer repositories; rejected here because ADR-0128 (accepted 2026-10-15)
  gives extensions their own repository and release cadence. Revisited only if ADR-0128 is.
- **Git submodules or subtree for the platform's core dependency.** Rejected: a submodule pins
  a commit that is forgotten, a subtree re-imports history; a released version with a lock is
  the dependency the platform's image already uses.
- **Developing in three repositories before the flip.** Rejected: the carve force-pushes the
  platform repository until the flip, so a direct commit there would be overwritten (the
  2026-10-14 decision stands until D9 step (5)).

## Decisions the owner took on acceptance (2026-10-16: every recommendation)

1. **The switch point.** Recommendation: at the flip (D9), not before.
2. **Release cadence.** Recommendation: a patch release whenever the platform needs a core
   change, and at least weekly while the platform moves fast; nightly prereleases to a dev
   index only if that proves too slow.
3. **The extensions repository.** Recommendation: dormant until the adapters move (ADR-0128);
   two active repositories at the switch.
4. **`cairn-dev`'s visibility.** Recommendation: private (it holds the maintainers' process
   files and agent conventions); the public repository carries the scrubbed copies.
5. **Issue migration.** Recommendation: recreate by script with back-links; do not keep
   `evs-cmd/cairn` as a live tracker.
6. **CircleCI.** Recommendation: removed from the public repository at the flip (D8); the
   platform decides separately once its Actions billing is fixed.
7. **Review turnaround for contributors.** Recommendation: first response within five working
   days, stated in `GOVERNANCE.md`.
