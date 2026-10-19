# ADR-0136 — CI after the cutover: CircleCI validates the platform, GitHub Actions the public core

- **Status:** ACCEPTED 2026-10-19 by the owner (decisions "3a", "5a" and "6a" of the ADR-0132
  cutover review).
- **Date:** 2026-10-19
- **Issues:** #2430 (the flip), #2696 (the cutover follow-ups).
- **Resolves** ADR-0132 owner decision 6 ("the platform decides separately"). **Keeps** ADR-0135
  D3 and ADR-0132 D8 (the public repository validates on its own GitHub Actions). **Implements**
  ADR-0132 D4's early-warning job and D8's `v*` tag protection.

## Context

1. ADR-0132 D8 moved the public repository's validation to GitHub Actions and left the
   platform's CI open (owner decision 6), because the `evs-cmd` organisation's Actions billing
   failed every job in 0 s.
2. ADR-0132 D4 wants an early warning: the platform's suite run against the core built from
   `cairndev-sh/cairn` `main`, so a core change that breaks the platform shows before the release
   that would carry it. A warning nobody has to read does not warn.
3. The public repository's release workflow is armed: a `v*` tag publishes every wheel (ADR-0132
   D5, D8). Branch protection does not cover tags.

## Decision

**D1 — CircleCI is the platform's CI.** `evs-cmd/cairn-platform` validates on CircleCI from its
`.circleci/config.yml` (authored in the monorepo at `scripts/oss/platform/circleci/` and placed
by the carve until the monorepo freezes, ADR-0132 D2): `python` (the platform suite, lint, the
console's API contract and `cairn pack check` on the pinned core), `core-main` (the same on the
core from `cairndev-sh/cairn` `main`), `packs` (every member builds) and `web` (the console). A
scheduled pipeline named `core-main-nightly` runs `core-main` on `main` every night. The CircleCI
project stays private: the `packs` job stores the platform's wheels as artifacts.

**D2 — The public repository validates on GitHub Actions only** (ADR-0135 D3): `ci.yml`'s
`public` and `public-e2e` jobs and the `dco` check are its required checks; it has no CircleCI
configuration and no CircleCI project.

**D3 — `core-main` is a required check on the platform's pull requests, and never blocks a core
release.** A platform pull request merges only when the platform passes on the pinned core AND on
the core's `main`, so the platform is ready for the next core release before it is cut. The
dependency runs one way: nothing in the public repository's release reads the platform's CI, so a
red `core-main` holds platform merges until the platform (or a core fix on `main`) makes it
green, and a core release proceeds regardless.

**D4 — Only a repository admin creates, moves or deletes a `v*` tag on the public repository.**
The tag ruleset `release tags v*` (id 25407950, applied 2026-10-19, enforcement active) targets
`refs/tags/v*` and restricts creation, update, deletion and non-fast-forward; the repository
admin role bypasses it. With the `pypi` environment's required reviewer, a release needs two
deliberate acts by the owner: the tag and the approval.

## Consequences

- The platform has CI from its first commit after the cutover, independent of the Actions
  billing of its organisation.
- A core change on `main` that breaks the platform stops platform merges until the platform
  adapts. That is the intent of D3: the break is fixed while it is one commit old, not when a
  release forces it. A long-lived break is a reason to fix or revert the core change, never to
  drop the required check.
- A contributor cannot publish a release by pushing a tag; a maintainer without the admin role
  cannot either.

## Verification

- The carved platform tree's `.circleci/config.yml` is well-formed CircleCI 2.1, runs no public
  core suite and names only paths the platform tree has
  (`tests/scripts/test_platform_circleci.py`, in both layouts); the `python` job's commands pass
  on a carved tree (#2430 part B proof).
- `core-main`'s installer refuses a run where any core distribution is not installed from the
  checkout (`install_core_main.py`, #2696).
- After the owner connects CircleCI to `evs-cmd/cairn-platform`: the first pipeline runs all four
  jobs, the nightly schedule exists, and the platform's branch protection lists `python` and
  `core-main` as required.
- `gh api repos/cairndev-sh/cairn/rulesets/25407950` reads back the ruleset of D4.

## Alternatives rejected

- **GitHub Actions for the platform.** Its organisation's Actions billing fails every job; the
  platform would have no CI at the cutover.
- **`core-main` as an advisory check.** Cheaper to live with, but an advisory red is read only
  when a release breaks the platform, which is what ADR-0132 D4 exists to prevent.
- **Tag protection by convention.** The release workflow cannot tell a deliberate tag from a
  mistaken one, and a published version can never be re-uploaded.
